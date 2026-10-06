const assert = require('node:assert/strict');
const test = require('node:test');
const { prepareRelease, completeRelease } = require('../.github/scripts/release.cjs');

const SHA = 'a'.repeat(40);
const OTHER = 'b'.repeat(40);
const DIGEST = `sha256:${'c'.repeat(64)}`;
const IMAGE = 'samuelnygaard/mailbridge';
const context = (ref = 'refs/heads/main', eventName = 'push') => ({
  repo: { owner: 'owner', repo: 'bridge' }, sha: SHA, ref, eventName,
});

// The GitHub API is the only external boundary; reservations survive reruns.
function fixture({ tags = [], releases = [], head = SHA } = {}) {
  const writes = [];
  const github = {
    paginate: async (method, options) => {
      assert.equal(method, github.rest.repos.listTags);
      assert.equal(options.per_page, 100);
      return tags.map(tag => ({ ...tag, commit: { ...tag.commit } }));
    },
    rest: {
      git: {
        getRef: async ({ ref }) => {
          assert.equal(ref, 'heads/main');
          return { data: { object: { sha: head } } };
        },
        createRef: async (options) => {
          if (tags.some(tag => `refs/tags/${tag.name}` === options.ref)) {
            throw Object.assign(new Error('reference exists'), { status: 422 });
          }
          writes.push(['tag', options]);
          tags.push({ name: options.ref.slice('refs/tags/'.length), commit: { sha: options.sha } });
        },
      },
      repos: {
        listTags: async () => { throw new Error('Use pagination'); },
        getReleaseByTag: async ({ tag }) => {
          const release = releases.find(release => release.tag_name === tag);
          if (!release) throw Object.assign(new Error('not found'), { status: 404 });
          return { data: release };
        },
        createRelease: async (options) => {
          writes.push(['release', options]);
          const release = { id: releases.length + 1, ...options };
          releases.push(release);
          return { data: release };
        },
        updateRelease: async (options) => {
          writes.push(['update', options]);
          Object.assign(releases.find(release => release.id === options.release_id), options);
        },
      },
    },
  };
  return { github, writes, tags, releases };
}

const tag = (name, sha = OTHER) => ({ name, commit: { sha } });
const prepare = state => prepareRelease({ github: state.github, context: context(), image: IMAGE });
const complete = (state, release) => completeRelease({
  github: state.github, context: context(), image: IMAGE, ...release, digest: DIGEST,
});

test('first main push reserves 0.1.3 from the 0.1.2 baseline', async () => {
  const state = fixture();
  const result = await prepare(state);
  assert.deepEqual(result, { publish: 'true', version: '0.1.3', tag: 'v0.1.3' });
  assert.equal(state.writes.length, 1);
  assert.equal(state.writes[0][1].sha, SHA);
  assert.equal(state.releases.length, 0, 'No release before Docker publishing succeeds');
});

test('increments the numerically highest stable version across all tag pages', async () => {
  const state = fixture({ tags: [tag('v0.9.8'), tag('v0.10.9'), tag('v1.0.0-rc.1'), tag('other'), tag('v0.01.4')] });
  assert.equal((await prepare(state)).version, '0.10.10');
});

test('an existing baseline tag advances to 0.1.3', async () => {
  assert.equal((await prepare(fixture({ tags: [tag('v0.1.2')] }))).version, '0.1.3');
});

test('retry after a failed upload reuses the reservation without incrementing', async () => {
  const state = fixture();
  const first = await prepare(state);
  assert.deepEqual(await prepare(state), first);
  assert.equal(state.writes.length, 1);
});

test('successful upload creates release notes and makes reruns skip publishing', async () => {
  const state = fixture();
  const selected = await prepare(state);
  await complete(state, selected);
  const release = state.releases[0];
  assert.equal(release.tag_name, 'v0.1.3');
  assert.equal(release.target_commitish, SHA);
  assert.equal(release.generate_release_notes, true);
  assert.ok(release.body.includes(`${IMAGE}@${DIGEST}`));
  assert.equal((await prepare(state)).publish, 'false');
  await complete(state, selected);
  assert.equal(state.writes.length, 2, 'Tag and release are each written once');
});

test('the next commit increments after a successful release', async () => {
  const state = fixture();
  await complete(state, await prepare(state));
  state.tags[0].commit.sha = OTHER;
  assert.equal((await prepare(state)).version, '0.1.4');
});

test('a release created by a maintainer is still published and preserves its notes', async () => {
  const state = fixture({ tags: [tag('v0.1.3', SHA)], releases: [
    { id: 7, tag_name: 'v0.1.3', body: 'Maintainer notes', draft: false },
  ] });
  const selected = await prepare(state);
  assert.equal(selected.publish, 'true');
  await complete(state, selected);
  assert.ok(state.releases[0].body.startsWith('Maintainer notes'));
  assert.equal(state.writes[0][0], 'update');
});

test('older main runs cannot reserve a version or publish latest', async () => {
  const state = fixture({ head: OTHER });
  assert.equal((await prepare(state)).publish, 'false');
  assert.equal(state.writes.length, 0);
});

test('manual main dispatch uses the same idempotent version selection', async () => {
  const state = fixture();
  const selected = await prepare(state);
  assert.deepEqual(await prepareRelease({ github: state.github,
    context: context('refs/heads/main', 'workflow_dispatch'), image: IMAGE }), selected);
});

test('explicit stable and prerelease tags are preserved without allocating another tag', async () => {
  for (const name of ['v0.2.0', 'v0.2.0-rc.1']) {
    const state = fixture({ tags: [tag(name, SHA)] });
    const ctx = context(`refs/tags/${name}`);
    const result = await prepareRelease({ github: state.github, context: ctx, image: IMAGE });
    assert.equal(result.version, name.slice(1));
    assert.equal(state.writes.length, 0);
    await completeRelease({ github: state.github, context: ctx, image: IMAGE, ...result, digest: DIGEST });
    assert.equal(state.releases[0].prerelease, name.includes('-'));
    assert.equal(state.releases[0].make_latest, 'false', 'Explicit tags do not replace the automatic latest release');
  }
});

test('main promotes a completed explicit-tag image without rebuilding or allocating another version', async () => {
  const state = fixture({ tags: [tag('v0.2.0', SHA)] });
  const ctx = context('refs/tags/v0.2.0');
  const selected = await prepareRelease({ github: state.github, context: ctx, image: IMAGE });
  await completeRelease({ github: state.github, context: ctx, image: IMAGE, ...selected, digest: DIGEST });
  const main = await prepare(state);
  assert.equal(main.publish, 'true', 'The explicit release did not publish latest');
  assert.equal(main.digest, DIGEST, 'Promote the already published manifest instead of rebuilding');
  assert.equal(main.version, '0.2.0');
  await complete(state, main);
  assert.equal(state.releases[0].make_latest, 'true');
  assert.equal((await prepare(state)).publish, 'false');
  assert.equal(state.tags.length, 1);
});

test('untrusted events, malformed tags and mismatched commits never write', async () => {
  for (const ctx of [context('refs/heads/main', 'pull_request'), context('refs/heads/feature', 'workflow_dispatch'),
    context('refs/tags/vbad'), context('refs/tags/v0.1.3-01'), context('refs/tags/v0.1.3+build'),
    context('refs/tags/v0.1.3')]) {
    const state = fixture({ tags: [tag('v0.1.3', OTHER)] });
    await assert.rejects(prepareRelease({ github: state.github, context: ctx, image: IMAGE }));
    assert.equal(state.writes.length, 0);
  }
});

test('API failures fail closed instead of starting again from the baseline', async () => {
  const state = fixture();
  state.github.paginate = async () => { throw new Error('API unavailable'); };
  await assert.rejects(prepare(state), /API unavailable/);
  assert.equal(state.writes.length, 0);
});

test('an external tag collision fails without overwriting the existing tag', async () => {
  const state = fixture();
  state.github.rest.git.createRef = async () => { throw Object.assign(new Error('collision'), { status: 422 }); };
  await assert.rejects(prepare(state));
  assert.equal(state.releases.length, 0);
});

test('failed release creation can retry the reserved version', async () => {
  const state = fixture();
  const selected = await prepare(state);
  const create = state.github.rest.repos.createRelease;
  state.github.rest.repos.createRelease = async () => { throw new Error('API unavailable'); };
  await assert.rejects(complete(state, selected), /API unavailable/);
  assert.deepEqual(await prepare(state), selected);
  state.github.rest.repos.createRelease = create;
  await complete(state, selected);
  assert.equal((await prepare(state)).publish, 'false');
});

test('an absent or invalid Docker digest never creates a release', async () => {
  const state = fixture();
  const selected = await prepare(state);
  await assert.rejects(completeRelease({ github: state.github, context: context(), image: IMAGE,
    ...selected, digest: '' }));
  assert.equal(state.releases.length, 0);
});

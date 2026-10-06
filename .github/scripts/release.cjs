// Git tags reserve versions before upload; a release marker records completion.
const BASELINE = [0, 1, 2];
const NUMBER = '(?:0|[1-9][0-9]*)';
const IDENTIFIER = `(?:${NUMBER}|[0-9]*[A-Za-z-][0-9A-Za-z-]*)`;
const VERSION = new RegExp(`^v(${NUMBER})\\.(${NUMBER})\\.(${NUMBER})(?:-(${IDENTIFIER}(?:\\.${IDENTIFIER})*))?$`);

function parseTag(name) {
  const match = VERSION.exec(name);
  if (!match) return null;
  const parts = match.slice(1, 4).map(Number);
  if (!parts.every(Number.isSafeInteger)) return null;
  return { tag: name, version: name.slice(1), parts, prerelease: Boolean(match[4]) };
}

function compare(left, right) {
  for (let index = 0; index < 3; index++) {
    if (left[index] !== right[index]) return left[index] - right[index];
  }
  return 0;
}

function validateContext(context) {
  if (!['push', 'workflow_dispatch'].includes(context.eventName) ||
      !(context.ref === 'refs/heads/main' || context.ref.startsWith('refs/tags/v')) ||
      !/^[a-f0-9]{40}$/.test(context.sha)) {
    throw new Error('Release publishing requires a main or version-tag push/dispatch and a commit SHA.');
  }
}

async function findRelease(github, repo, tag) {
  try {
    return (await github.rest.repos.getReleaseByTag({ ...repo, tag })).data;
  } catch (error) {
    if (error.status === 404) return null;
    throw error;
  }
}

function publicationPattern(image, sha) {
  const escapedImage = image.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  return new RegExp(`^<!-- mailbridge-image: ${escapedImage}@(sha256:[a-f0-9]{64}); commit=${sha}; latest=(done|untouched) -->$`, 'm');
}

function publishedImage(release, image, sha) {
  const match = release && !release.draft && (release.body || '').match(publicationPattern(image, sha));
  return match ? { digest: match[1], latest: match[2] === 'done' } : null;
}

async function prepareRelease({ github, context, image }) {
  validateContext(context);
  const main = context.ref === 'refs/heads/main';
  if (main) {
    const head = await github.rest.git.getRef({ ...context.repo, ref: 'heads/main' });
    if (head.data.object.sha !== context.sha) {
      return { publish: 'false', version: '', tag: '' };
    }
  }
  const tags = await github.paginate(github.rest.repos.listTags, { ...context.repo, per_page: 100 });
  let selected;
  if (main) {
    const stable = tags.map(tag => ({ ...parseTag(tag.name), sha: tag.commit.sha }))
      .filter(tag => tag.parts && !tag.prerelease);
    selected = stable.filter(tag => tag.sha === context.sha)
      .sort((a, b) => compare(b.parts, a.parts))[0];
    if (!selected) {
      const highest = stable.reduce((latest, tag) => compare(tag.parts, latest) > 0 ? tag.parts : latest, BASELINE);
      const next = [...highest];
      next[2]++;
      selected = parseTag(`v${next.join('.')}`);
      if (!selected) throw new Error('The next patch version exceeds the supported integer range.');
      // Atomic creation fails on collisions; never overwrite an existing tag.
      await github.rest.git.createRef({ ...context.repo, ref: `refs/tags/${selected.tag}`, sha: context.sha });
    }
  } else {
    selected = parseTag(context.ref.slice('refs/tags/'.length));
    if (!selected) throw new Error('Use vMAJOR.MINOR.PATCH or vMAJOR.MINOR.PATCH-PRERELEASE; build metadata is unsupported.');
    if (!tags.some(tag => tag.name === selected.tag && tag.commit.sha === context.sha)) {
      throw new Error('The release tag does not reference this commit.');
    }
  }
  const release = await findRelease(github, context.repo, selected.tag);
  const published = publishedImage(release, image, context.sha);
  if (main && published && !published.latest) {
    return { publish: 'true', version: selected.version, tag: selected.tag, digest: published.digest };
  }
  return {
    publish: published ? 'false' : 'true',
    version: selected.version, tag: selected.tag,
  };
}

async function completeRelease({ github, context, image, version, digest }) {
  validateContext(context);
  const selected = parseTag(`v${version}`);
  if (!selected || !/^sha256:[a-f0-9]{64}$/.test(digest || '')) {
    throw new Error('A valid release version and successful Docker image digest are required.');
  }
  const tags = await github.paginate(github.rest.repos.listTags, { ...context.repo, per_page: 100 });
  if (!tags.some(tag => tag.name === selected.tag && tag.commit.sha === context.sha)) {
    throw new Error('The reserved release tag no longer references this commit.');
  }
  const release = await findRelease(github, context.repo, selected.tag);
  const main = context.ref === 'refs/heads/main';
  const published = publishedImage(release, image, context.sha);
  if (published && (!main || published.latest)) return;
  if (published && published.digest !== digest) throw new Error('Promote the previously recorded image digest.');
  const marker = `<!-- mailbridge-image: ${image}@${digest}; commit=${context.sha}; latest=${main ? 'done' : 'untouched'} -->`;
  const options = {
    ...context.repo, draft: false, prerelease: selected.prerelease,
    make_latest: main ? 'true' : 'false',
  };
  if (release) {
    const body = (release.body || '').replace(publicationPattern(image, context.sha), '').trim();
    await github.rest.repos.updateRelease({
      ...options, release_id: release.id, body: `${body}\n\n${marker}`.trim(),
    });
  } else {
    await github.rest.repos.createRelease({
      ...options, tag_name: selected.tag, target_commitish: context.sha,
      name: selected.tag, generate_release_notes: true, body: marker,
    });
  }
}

module.exports = { prepareRelease, completeRelease };

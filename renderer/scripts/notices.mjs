// Third-party notices for what the image actually ships — generated from the
// pruned production node_modules, never hand-maintained, so a dependency that
// arrives with the lockfile arrives in the notices in the same commit.
//
// Output: Markdown on stdout — one section per package with its declared
// licence and the full text of every LICENSE/NOTICE/COPYING file it carries.
// A package that declares no licence is listed under a heading of its own
// rather than skipped: that is the row a reviewer has to look at.
import {readdirSync, readFileSync, existsSync} from 'node:fs';
import {join} from 'node:path';

const root = process.argv[2] ?? 'node_modules';
const packages = new Map();

const visit = (dir) => {
  if (!existsSync(dir)) return;
  for (const entry of readdirSync(dir, {withFileTypes: true})) {
    if (!entry.isDirectory() || entry.name.startsWith('.')) continue;
    const full = join(dir, entry.name);
    if (entry.name.startsWith('@')) {
      visit(full);
      continue;
    }
    const manifest = join(full, 'package.json');
    if (existsSync(manifest)) {
      const pkg = JSON.parse(readFileSync(manifest, 'utf8'));
      if (pkg.name && pkg.version) {
        const key = `${pkg.name}@${pkg.version}`;
        if (!packages.has(key)) packages.set(key, {pkg, dir: full});
      }
    }
    visit(join(full, 'node_modules'));
  }
};
visit(root);

const licenceOf = (pkg) =>
  typeof pkg.license === 'string'
    ? pkg.license
    : pkg.license?.type ?? (Array.isArray(pkg.licenses) ? pkg.licenses.map((l) => l.type).join(' OR ') : null);

const out = ['# Third-party notices — tamtree-remotion-render', ''];
out.push(`Generated from the image's production \`node_modules\` (${packages.size} packages).`, '');
const unlicensed = [];
for (const [key, {pkg, dir}] of [...packages].sort(([a], [b]) => a.localeCompare(b))) {
  const licence = licenceOf(pkg);
  if (!licence) unlicensed.push(key);
  out.push(`## ${key}`, '', `Licence: ${licence ?? '**UNDECLARED**'}`, '');
  for (const file of readdirSync(dir)) {
    if (/^(licen[cs]e|notice|copying)(\.|$)/i.test(file)) {
      out.push('```', readFileSync(join(dir, file), 'utf8').trim(), '```', '');
    }
  }
}
out.push('## Packages declaring no licence', '');
out.push(unlicensed.length ? unlicensed.map((k) => `- ${k}`).join('\n') : 'None.', '');
process.stdout.write(out.join('\n'));

import { readdir, readFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

const dist = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../dist");

const patterns = [
  { name: "execute-api host", re: /execute-api/i },
  { name: "amazoncognito.com host", re: /amazoncognito\.com/i },
  { name: "Cognito client id", re: /(?<![a-z0-9])[a-z0-9]{26}(?![a-z0-9])/ },
];

const files = await javascriptFiles(dist);
if (files.length === 0) {
  console.error(`No built JavaScript under ${dist}. Run npm run build in web/ first.`);
  process.exit(1);
}

const hits = [];
for (const file of files) {
  const source = await readFile(file, "utf8");
  for (const pattern of patterns) {
    if (pattern.re.test(source)) {
      hits.push(`${path.relative(dist, file)}: ${pattern.name}`);
    }
  }
}

if (hits.length) {
  console.error("The production bundle contains stage-specific values:");
  for (const hit of hits) {
    console.error(`  ${hit}`);
  }
  process.exit(1);
}

console.log(`Checked ${files.length} JavaScript file(s) in dist/ for stage values.`);

async function javascriptFiles(dir) {
  let entries;
  try {
    entries = await readdir(dir, { withFileTypes: true });
  } catch {
    return [];
  }
  const found = [];
  for (const entry of entries) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      found.push(...(await javascriptFiles(full)));
    } else if (entry.name.endsWith(".js")) {
      found.push(full);
    }
  }
  return found;
}

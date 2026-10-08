// Every UI string exists in every language (NFR-06): kk.json must have exactly the keys of the
// reference ru.json, and ICU placeholders must match. Run by `pnpm typecheck` (make check).
import { readFileSync } from "node:fs";

const load = (l) => JSON.parse(readFileSync(new URL(`../messages/${l}.json`, import.meta.url), "utf8"));
const flat = (o, p = "", out = {}) => {
  for (const [k, v] of Object.entries(o)) {
    if (p === "" && k === "meta") continue;
    const key = p ? `${p}.${k}` : k;
    if (v && typeof v === "object") flat(v, key, out);
    else out[key] = String(v);
  }
  return out;
};
const args = (s) => [...s.matchAll(/\{(\w+)/g)].map((m) => m[1]).filter((a) => a !== "count" || s.includes("{count")).sort().join(",");
const ref = flat(load("ru"));
let errors = 0;
for (const lang of ["kk"]) {
  const msgs = flat(load(lang));
  for (const k of Object.keys(ref)) {
    if (!(k in msgs)) {
      console.error(`${lang}: missing ${k}`);
      errors++;
    } else if (args(ref[k]) !== args(msgs[k])) {
      console.error(`${lang}: placeholders differ in ${k}: [${args(ref[k])}] vs [${args(msgs[k])}]`);
      errors++;
    }
  }
  for (const k of Object.keys(msgs)) {
    if (!(k in ref)) {
      console.error(`${lang}: extra ${k}`);
      errors++;
    }
  }
}
if (errors) process.exit(1);
console.log(`messages: ${Object.keys(ref).length} keys, ru/kk consistent`);

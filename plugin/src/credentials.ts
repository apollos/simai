import { lstatSync, readFileSync } from "node:fs";

/** Read a plugin credential only from an owner-only regular file. */
export function readOwnerOnlyToken(path: string): string {
  const stat = lstatSync(path);
  if (!stat.isFile() || stat.isSymbolicLink()) {
    throw new Error("simai: token path must be a regular file, not a symlink");
  }
  if (typeof process.geteuid === "function" && stat.uid !== process.geteuid()) {
    throw new Error("simai: token file must be owned by the current user");
  }
  if ((stat.mode & 0o077) !== 0) {
    throw new Error("simai: token file must not grant group/world permissions");
  }
  const token = readFileSync(path, "utf-8").trim();
  if (token.length < 32) throw new Error("simai: invalid token file");
  return token;
}

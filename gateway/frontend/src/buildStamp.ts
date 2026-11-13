import type { Plugin } from "vite";

// The app has no node types (it is a browser app), so the build step loads
// the node modules by a name the type checker does not resolve.
// eslint-disable-next-line @typescript-eslint/no-explicit-any
const node = (name: string): Promise<any> => import(/* @vite-ignore */ name);

export const PLACEHOLDER = "__BUILD_ID__";

// The text of the worker with the id of the build in place of the placeholder.
export function stampWorker(source: string, buildId: string): string {
  return source.split(PLACEHOLDER).join(buildId);
}

// The id of a build: BUILD_ID when set, else a hash of the built index page.
// The page names every hashed asset, so any change of the app changes the id.
export async function buildIdOf(index: string, env: string | undefined): Promise<string> {
  const set = (env ?? "").trim().replace(/[^A-Za-z0-9._-]/g, "");
  if (set) return set;
  const { createHash } = await node("node:crypto");
  return createHash("sha256").update(index).digest("hex").slice(0, 12);
}

// After the build has copied `public/`, stamp `dist/sw.js`.
export function buildStamp(): Plugin {
  let outDir = "dist";
  return {
    name: "joshua-build-stamp",
    apply: "build",
    configResolved(config) {
      outDir = `${config.root}/${config.build.outDir}`;
    },
    async closeBundle() {
      const { existsSync, readFileSync, writeFileSync } = await node("node:fs");
      const worker = `${outDir}/sw.js`;
      const index = `${outDir}/index.html`;
      if (!existsSync(worker) || !existsSync(index)) return;
      const env = (await node("node:process")).env.BUILD_ID as string | undefined;
      const id = await buildIdOf(readFileSync(index, "utf8"), env);
      writeFileSync(worker, stampWorker(readFileSync(worker, "utf8"), id));
    },
  };
}

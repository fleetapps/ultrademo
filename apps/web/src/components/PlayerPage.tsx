import { notFound } from "next/navigation";
import { type CSSProperties, cache } from "react";

import { fetchLaunchConfig } from "@/lib/api";
import { launchParams } from "@/lib/launch";
import { accentVars } from "@/lib/theme";
import { Player } from "./Player";

type Search = Record<string, string | string[] | undefined>;

// One API read per request, shared by generateMetadata and the page (React `cache`).
export const getConfig = cache((slug: string, test: boolean) => fetchLaunchConfig(slug, test));

export function isTest(search: Search): boolean {
  return launchParams(search).test;
}

export async function PlayerPage({ slug, search, embed }: { slug: string; search: Search; embed: boolean }) {
  const launch = launchParams(search);
  const config = await getConfig(slug, launch.test);
  if (!config) notFound();
  return (
    <div
      className={embed ? "h-dvh overflow-auto" : "min-h-dvh"}
      style={accentVars(config.branding.accent) as CSSProperties}
    >
      <Player config={config} launch={launch} embed={embed} />
    </div>
  );
}

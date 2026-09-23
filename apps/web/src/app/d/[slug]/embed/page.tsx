import type { Metadata } from "next";

import { getConfig, isTest, PlayerPage } from "@/components/PlayerPage";

// Framed by customers' pages. proxy.ts sets frame-ancestors to the launch config's embed origins.

export async function generateMetadata(props: PageProps<"/d/[slug]/embed">): Promise<Metadata> {
  const { slug } = await props.params;
  const config = await getConfig(slug, isTest(await props.searchParams));
  return config ? { title: config.name } : {};
}

export default async function EmbedPage(props: PageProps<"/d/[slug]/embed">) {
  const { slug } = await props.params;
  return <PlayerPage slug={slug} search={await props.searchParams} embed />;
}

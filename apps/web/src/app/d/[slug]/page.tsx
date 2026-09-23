import type { Metadata } from "next";

import { getConfig, isTest, PlayerPage } from "@/components/PlayerPage";

export async function generateMetadata(props: PageProps<"/d/[slug]">): Promise<Metadata> {
  const { slug } = await props.params;
  const config = await getConfig(slug, isTest(await props.searchParams));
  return config ? { title: config.name, description: config.description || undefined } : {};
}

export default async function DemoPage(props: PageProps<"/d/[slug]">) {
  const { slug } = await props.params;
  return <PlayerPage slug={slug} search={await props.searchParams} embed={false} />;
}

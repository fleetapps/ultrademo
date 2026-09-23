import type { Cta } from "./ctas";

// What the server page hands the client player: the public launch config plus request details.

export interface PublicFormField {
  name: string;
  label: string;
  type: "text" | "email" | "tel" | "select";
  required: boolean;
  options?: string[] | null;
}

export interface PublicLaunchConfig {
  slug: string;
  name: string;
  description: string;
  status: string;
  agent_name: string;
  product_name: string;
  ctas: Cta[];
  branding: { accent?: string; logo_url?: string };
  form: { fields: PublicFormField[] } | null;
  embed_origins: string[];
  max_duration_s: number;
}

export interface StartResponse {
  session_id: string;
  livekit_url: string;
  token: string;
  receipt: string;
}

export interface LaunchParams {
  token: string | null;
  test: boolean;
  params: Record<string, string>;
}

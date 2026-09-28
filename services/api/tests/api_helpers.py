def bearer(key: str) -> dict[str, str]:
    return {"authorization": f"Bearer {key}"}


INTERNAL = bearer("test-internal")


def spec(slug: str, status: str = "published", **launch_config) -> dict:
    return {
        "organization": {"slug": slug, "name": slug.title()},
        "product": {
            "name": f"{slug} CRM",
            "base_url": f"https://app.{slug}.example",
            "allowed_domains": [f"app.{slug}.example"],
        },
        "agent": {"name": "Ava", "system_prompt": f"You demo the {slug} CRM."},
        "launch_config": {
            "slug": f"{slug}-demo",
            "name": f"{slug} demo",
            "status": status,
            "ctas": [{"kind": "book", "label": "Book a call", "url": "https://cal.example/x"}],
            **launch_config,
        },
    }

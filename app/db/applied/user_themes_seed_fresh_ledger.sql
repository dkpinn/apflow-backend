-- ============================================================
-- User Themes Seed: Fresh Ledger
-- Adds the default purchased-theme candidate used by the frontend
-- Themes settings tab. Idempotent.
-- ============================================================

insert into public.themes (slug, name, description, tokens)
values (
  'fresh-ledger',
  'Fresh Ledger',
  'Clean default accounting theme',
  '{
    "colors": {
      "background": "#ffffff",
      "surface": "#f8fafc",
      "text": "#0f172a",
      "text_muted": "#64748b",
      "border": "#e2e8f0",
      "primary": "#2563eb",
      "primary_text": "#ffffff",
      "accent": "#14b8a6"
    },
    "typography": {
      "family": "system",
      "heading_weight": 600
    },
    "density": "comfortable",
    "radius": "md",
    "shadow": "sm"
  }'::jsonb
)
on conflict (slug) do update
set
  name = excluded.name,
  description = excluded.description,
  tokens = excluded.tokens,
  is_active = true,
  updated_at = now();

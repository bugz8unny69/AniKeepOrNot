# Settings schema and generated dashboard

Ordinary application settings are defined in one registry:
`src/legacy/anime_settings_schema.py` (`SCHEMA`). The configuration loader
derives `DEFAULT_SETTINGS` from that registry. The dashboard uses the same
registry to render every visible scalar field.

## Adding a setting

Add the field to the appropriate section under `SCHEMA`:

```python
"new_option": field(5, label="New option", minimum=1, maximum=20)
```

For dashboard-enabled sections, an integer input will automatically appear on
the Settings page and be submitted to the existing `/api/settings` endpoint.
Backend schema validation enforces integer/string/boolean type, numeric bounds
and enumerated choices. Existing complex cross-field validators still run in
`anime_flat_config.validate_config`.

For sensitive or complex/internal fields, set `visible=False`. Dictionaries
such as `identity.episode_map` must use a custom editor and cannot appear
as automatically generated inputs. A new section must be deliberately marked
`dashboard=True` to be exposed. A plain client name, such as
`anidb.client`, is ordinary configuration. API keys and passwords are
**never** added as regular setting fields: they use the separate secrets
system and a section's `credential` key.

Only configured integrations with existing safe probe logic have a
`test_connection=True` control. AniDB does not expose that control: a test
button should not consume the API's scarce request budget. Register your
AniDB HTTP API client outside AniKeepOrNot, then enter the client name
and version in the AniDB section of the dashboard.

Saving validates the entire file and persists it atomically. Dashboard-visible
settings and changed credentials are loaded automatically: the web server reads
the current webhook token on each request, acquisition workers refresh valid
settings between jobs, and identity/NTFY/Sonarr/Shoko monitors wake when the
settings or secrets file changes (typically within a second). Jobs already
running complete with their originally claimed configuration; no running
download is canceled merely because a setting was edited.

The dashboard keeps **Save & Apply** on each integration, removes the redundant
Refresh button, and displays a confirmation that changes apply to new work.
Infrastructure options such as the web listening port, daemon concurrency, or
storage paths are not dashboard-editable and may still require a controlled
restart when changed through the CLI. The backend explicitly reports
`restart_required` for these fields.

The original manual Keep→Sonarr→Shoko boundary is unchanged. Enabling Sonarr
or Shoko does not bypass Keep: their monitors remain idle while disabled.

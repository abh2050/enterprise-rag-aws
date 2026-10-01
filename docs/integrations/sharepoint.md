# SharePoint / OneDrive connector (Microsoft Graph)

**Status:** implemented-not-live-tested → **blocked** (no Microsoft 365 tenant). Contract-tested with documented
payloads.

## Permissions
* `Files.Read.All` (application) for delta, permissions and content, or `Sites.Selected` with per-site grants
  (preferred least privilege). Admin consent is required.
* Detecting permission changes through delta headers needs `Sites.FullControl.All` per Graph docs. **Not requested.**
  Instead, permissions are re-read for each changed item and on periodic reconciliation.

## Translation rules (deny by default)
| Graph permission | Result |
|---|---|
| `grantedToV2.user.id` / `grantedToIdentitiesV2[].user.id` | user principal (Entra object id) |
| `grantedToV2.group.id` | group principal |
| Sharing `link` (anonymous/organization/users) | **unsupported → document denied** |
| `siteGroup` / `siteUser` without Entra id, `application` | **unsupported → document denied** |

Graph docs state that for non-owner callers only permissions applying to the caller are returned. Before enabling,
confirm in the target tenant that the app identity sees the complete permission set (live check).

## Live checks required
Delta paging and 410 resync, a permission-only change propagating within the sync interval, a deleted item → tombstone,
a labeled file → mapped label, and a double-key-encrypted file → quarantined.

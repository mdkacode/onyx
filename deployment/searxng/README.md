# Private SearXNG relay for Onyx web search

Gives NaArNi Gyan (Onyx) live information from the public web without handing a
search vendor a record of what the company is asking about, and without any
route by which internal data can travel outward.

[SearXNG](https://github.com/searxng/searxng) is a self-hosted metasearch
engine: it forwards a query to public engines (DuckDuckGo, Brave, Wikipedia,
…), merges the results, and returns them as JSON. Because we run it ourselves,
there is no API account, no vendor-side query log, and no per-search billing.

## The one-way boundary

```
   ┌──────────────────────────┐        ┌───────────────────────┐
   │  Onyx VM  (172.16.0.5)   │        │ SearXNG VM            │
   │                          │        │ (172.16.0.10)         │
   │  egress guard ──────────────────► │  ──────────────────────────►  public
   │  screens the query text  │  VNet  │  outbound only         │      engines
   │                          │  only  │                        │
   └──────────────────────────┘        └───────────────────────┘
             ▲                                    ▲
             │  results flow back                 │  X  nothing inbound:
             │                                    │     NSG denies Internet
                                                        and the rest of the VNet
```

Three independent controls, each of which holds on its own:

1. **Network.** The relay has no inbound path. Its NSG denies inbound from the
   internet *and* from every other VNet host, allow-listing only the Onyx VM on
   `:8080` (plus break-glass SSH from that same address). Its public IP exists
   purely so it can make outbound requests — Azure retired default outbound
   access, so a VM with no public IP could not reach search engines at all.

2. **Payload.** The only bytes that leave Onyx on this path are the search
   query strings, and those are written by an LLM whose context holds internal
   documents. Every query is screened first by
   [`egress_guard.py`](../../backend/onyx/tools/tool_implementations/web_search/egress_guard.py):
   credentials and key material are blocked outright, over-long queries and
   long quoted verbatim passages are blocked (both are how pasted document text
   escapes), and internal hostnames, private IPs, long digit runs and local
   filesystem paths are stripped. The UI shows the queries that actually went
   out, not the ones the model proposed.

3. **Retention.** The relay keeps nothing. Metrics are off, the limiter is off
   (so no Redis), only the JSON API is enabled — there is not even a browsable
   search UI — and container logs are capped at a single 1 MB file.

What still leaves, by necessity: the topic you are asking about, as a short
keyword query, attributed to an anonymous Azure IP. That is the irreducible
cost of asking the web a question.

**Fetching page content** stays on the built-in Onyx Web Crawler, which fetches
directly from our own network. Do not switch the content provider to Firecrawl
— that would send every URL Onyx opens to a third-party SaaS, reopening the
leak this design closes.

## Layout

```
deployment/searxng/
├── docker-compose.yml       single container; no Redis needed
├── config/settings.yml      SearXNG config (JSON-only, no metrics, no limiter)
├── register-provider.sh     point Onyx at the relay via the admin API
└── azure/
    ├── config.env           subscription / RG / VNet / VM targets
    ├── provision.sh         create everything (idempotent, --dry-run)
    ├── cloud-init.yaml      first-boot template (embeds the files above)
    ├── bootstrap.sh         runs on the VM at first boot
    ├── status.sh            read-only health + boundary check
    └── teardown.sh          delete everything, disks included
```

`provision.sh` embeds `docker-compose.yml`, `config/settings.yml` and
`bootstrap.sh` into cloud-init at deploy time, so this directory stays the
single source of truth — edit here, re-run, never hand-edit files on the VM.

## Deploy

```bash
cd deployment/searxng/azure
./provision.sh --dry-run     # print every az command, change nothing
./provision.sh               # create it (safe to re-run)
```

Then register it with Onyx, either in **Admin Panel → Web Search → SearXNG**
with base URL `http://172.16.0.10:8080`, or:

```bash
ONYX_API_KEY=on_... ../register-provider.sh
```

`provision.sh` finishes by proving both directions: that the Onyx VM can query
the relay over the VNet, and that the relay refuses connections from the public
internet.

## Operate

```bash
./azure/status.sh                          # health + boundary check
./azure/teardown.sh                        # delete it all, disks included

# Logs / shell without opening any inbound port:
az vm run-command invoke -g naarni-cad-vm_group -n naarni-searxng-vm \
  --command-id RunShellScript --scripts 'docker compose -f /opt/searxng/docker-compose.yml logs --tail=50'
```

The VM patches itself (unattended-upgrades) and refreshes the SearXNG image
weekly via a systemd timer — engine scrapers break as sites change, so a stale
image quietly degrades result quality over weeks.

## Cost

Central India, pay-as-you-go, INR:

| Item | | ₹/month |
| --- | --- | ---: |
| `Standard_B2ts_v2` | 2 vCPU / 1 GiB burstable | 782 |
| OS disk | 30 GB StandardSSD | ~190 |
| Public IP | Standard static, egress only | ~300 |
| | **Total** | **~1,270** |

`B2ts_v2` costs the same per hour as `B1s` but has twice the cores, and SearXNG
is latency-bound on parallel outbound HTTP, so cores help more than RAM. There
is no search-API bill on top: the engines are queried directly.

## Tuning result quality

Optional keys on the provider's `config` (settable via `register-provider.sh`
or the admin API; the admin form exposes only the base URL):

| Key | Default | Effect |
| --- | --- | --- |
| `num_results` | 20 | Results returned per query |
| `max_pages` | 3 | Pages fetched to fill `num_results` |
| `timeout_seconds` | 15 | Per-request timeout |
| `language` | — | e.g. `en`, `en-IN` |
| `time_range` | — | `day` / `week` / `month` / `year` — bias toward fresh sources |
| `categories` | — | e.g. `general`, `news`, `science` |
| `engines` | — | Restrict to specific engines, e.g. `duckduckgo,wikipedia` |
| `safesearch` | — | `0` off, `1` moderate, `2` strict |

Engine selection lives in `config/settings.yml` on the relay itself; it
inherits SearXNG's defaults via `use_default_settings: true` so upgrades keep
picking up upstream engine fixes.

## Guard configuration

Set on the Onyx backend (api_server + background workers):

| Variable | Default | Effect |
| --- | --- | --- |
| `WEB_SEARCH_EGRESS_GUARD_ENABLED` | `true` | Master switch |
| `WEB_SEARCH_MAX_QUERY_CHARS` | `256` | Longer queries are blocked |
| `WEB_SEARCH_MAX_QUOTED_WORDS` | `12` | Longer quoted spans are blocked |
| `WEB_SEARCH_INTERNAL_DOMAINS` | from `WEB_DOMAIN` | Hostnames stripped from queries |

The guard runs for **every** search provider, not just SearXNG, so the boundary
survives someone switching providers later.

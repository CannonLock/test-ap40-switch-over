# AP40: moving to the Origin's embedded issuer

Tested in a private Pelican federation built from the published v26.0 images (Registry, Director, AP40's Origin on
XRootD, a Cache), starting from AP40's real configuration. See [The test federation](#the-test-federation).

**The steps to follow are in [Transition guide](#transition-guide).** The sections before it explain why the steps
are what they are.

## Where we start

AP40's Origin today, from its running config (only the values that matter here):

```yaml
Server:
  Hostname: ap40.uw.osg-htc.org
  ExternalWebUrl: https://ap40.uw.osg-htc.org:8444
  IssuerUrl: https://osg-htc.org/ospool
Federation:
  DiscoveryUrl: https://osg-htc.org
Origin:
  StorageType: posix
  EnableIssuer: false
  IssuerMode: embedded
  Exports:
    - FederationPrefix: /ospool/ap40/
      StoragePrefix: /storage/ap40/
      Capabilities: [DirectReads, Writes, Reads, Listings]
    - FederationPrefix: /ospool/uw-shared/projects/
      StoragePrefix: /storage/uw-shared/projects/
      Capabilities: [DirectReads, Writes, Reads, Listings]
```

- **Issuers:** neither export names any, so both inherit `Server.IssuerUrl` and trust only
  `https://osg-htc.org/ospool`.
- **What gets the list:** the Origin advertises it to the Director, and the Origin's XRootD and every Cache's XRootD
  are configured from it.

## Where we end

Both exports trust one issuer, the `/ospool/ap40` embedded issuer:
`https://ap40.uw.osg-htc.org:8444/api/v1.0/issuer/ns/ospool/ap40`.

- **Keys:** it publishes the Origin's key and the AP's keys.
- **Tokens:** the AP mints every token with that `iss`.

## What the tests found

1. **Turning on the embedded issuer by itself is an outage** (stage 1a).
   - **What happens:** with `EnableIssuer: true` and no `IssuerUrls` on the exports, Pelican replaces the inherited
     `https://osg-htc.org/ospool` with a per-export embedded issuer. The Director, the Origin's XRootD and the
     Cache's XRootD all switch, and every existing AP token is refused, at the Origin and through the Cache, on both
     exports.
   - **What to do:** name the current issuer on every export in the same change (stage 1b).
2. **With AP40's trailing slashes, the default embedded issuer accepts no token at all** (stages 1a and 5b). There
   are two separate problems:
   - **Spelling mismatch.** For `FederationPrefix: /ospool/ap40/`, Pelican advertises and trusts
     `.../issuer/ns/ospool/ap40/` (with the slash), but the issuer names itself `.../issuer/ns/ospool/ap40`
     (discovery `issuer`). Tokens carrying the issuer's own name are refused.
   - **The slash spelling fails too.** A token carrying the slash spelling is also refused by XRootD. SciTokens
     looks up `<iss>/.well-known/openid-configuration`, which becomes `.../ns/ospool/ap40//.well-known/...`, and the
     Origin answers 404. XRootD's trace says "Failed to retrieve metadata provider information for issuer".
   - **XRootD differs from Pelican's data server here.** Pelican's own data server collapses the double slash and
     did accept that token.
   - **What to do:** write every `IssuerUrls` entry explicitly, without the trailing slash. Both problems are worth
     reporting to Pelican.
3. **`/ospool/uw-shared/projects` needs a decision.**
   - **The problem:** after step 4 the AP mints every token with the `/ospool/ap40` issuer, so `uw-shared` must
     trust that issuer too.
   - **What was tested:** listing it on both exports. That works at the Origin and through the Cache (stages 3, 5a
     and 5c).
   - **The alternative:** keep `uw-shared` on `osg-htc.org/ospool`, with the AP still minting old-`iss` tokens for
     it. That wasn't tested.
4. **The Cache behaves like the Origin.** It builds its XRootD issuer list from the Director's advertisement, and in
   every stage it accepted and refused exactly the tokens the Origin did.
   - **Issuer list per stage:** the generated XRootD configs show it. In 3, 5a and 5c, one issuer section covers both
     exports, `base_path = /ospool/ap40, /ospool/uw-shared/projects`, with XRootD dropping the trailing slash. Every
     stage's config is in `.sim/report/` after a run.
5. **`Server.IssuerUrl` can go once every export names its issuers** (stage 5c behaves exactly like 5a). The
   Director checks an Origin's advertisement against its key from the Registry, not against the token's `iss`
   (`director/origin_api.go`), so that setting doesn't affect advertising.
6. **Tokens are valid on both exports, as today.** Scope paths are relative to each export, and both exports trust
   the same issuer. So a token is valid on both, as old-issuer tokens already are (stage 0).

## Transition guide

Every issuer URL is written out in full below. Keep `https://ap40.uw.osg-htc.org:8444/api/v1.0/issuer/ns/ospool/ap40`
exactly as written: no trailing slash.

### 0. Before you start

- **Version:** the Origin runs 26.0.2 or later (the version tested).
- **Signing key:** `IssuerKeysDirectory` (`/etc/pelican/issuer-keys`) persists across restarts. The Origin's
  Registry registration depends on that key as well.

### 1. Turn on the embedded issuer, pinning today's issuer (`stages/1b-issuer-on-pinned.yaml`)

```yaml
Origin:
  EnableIssuer: true
  Exports:
    - FederationPrefix: /ospool/ap40/
      # ...
      IssuerUrls:
        - https://osg-htc.org/ospool
    - FederationPrefix: /ospool/uw-shared/projects/
      # ...
      IssuerUrls:
        - https://osg-htc.org/ospool
```

Then populate users, groups and collections. What's trusted is unchanged.

### 2. Publish the AP's keys under the ap40 embedded issuer (`stages/2-ap-keys-published.yaml`)

On the `/ospool/ap40/` export only:

```yaml
      IssuerJwks: /etc/pelican/ap40-issuer.jwks
```

- **Contents:** the file holds the JWKS that `osg-htc.org/ospool` serves for AP40 (`origin/ap40-issuer.jwks`).
- **Ownership:** it must be owned by the Pelican user and not writable by anyone else.
- **Never `Server.IssuerJwks`:** keys there are trusted server-wide, not just for this export.

**Check:**

```sh
curl -s https://ap40.uw.osg-htc.org:8444/api/v1.0/issuer/ns/ospool/ap40/.well-known/openid-configuration | jq '{issuer, jwks_uri}'
curl -s https://ap40.uw.osg-htc.org:8444/api/v1.0/issuer/ns/ospool/ap40/.well-known/issuer.jwks | jq '[.keys[].kid]'
curl -s https://ap40.uw.osg-htc.org:8444/.well-known/issuer.jwks | jq '[.keys[].kid]'
```

- **Namespace JWKS:** it lists `b9ee`, `5e49`, `abcc` and `778d` plus the Origin's own `kid`.
- **Server-wide JWKS:** it lists only the Origin's `kid`.

### 3. Trust both issuers on both exports, ap40 embedded first (`stages/3-both-issuers.yaml`)

```yaml
      IssuerUrls:
        - https://ap40.uw.osg-htc.org:8444/api/v1.0/issuer/ns/ospool/ap40
        - https://osg-htc.org/ospool
```

Use this same list on both exports. The first entry is where clients get new tokens, so CLI users of either export
are sent to the ap40 embedded issuer.

**Check, once the Origin has re-advertised** (one advertisement interval, 1 minute by default):

```sh
curl -sI https://<director>/ospool/ap40/<any object> | grep -i '^x-pelican-'
curl -sI https://<director>/ospool/uw-shared/projects/<any object> | grep -i '^x-pelican-'
```

- **Expected headers:** two `X-Pelican-Authorization` lines (ap40 embedded, then `osg-htc.org/ospool`), and
  `X-Pelican-Token-Generation: issuer=https://ap40.uw.osg-htc.org:8444/api/v1.0/issuer/ns/ospool/ap40, ...`.
- **Caches pick this up later.** Each cache rebuilds its XRootD token config from the Director on its own schedule
  (`Xrootd.AuthRefreshInterval`, 5 minutes by default), not immediately. The test restarted the Cache instead of
  waiting.
- **Old tokens:** confirm an existing AP token still reads through a cache.

### 4. Switch the AP's issuer to `https://ap40.uw.osg-htc.org:8444/api/v1.0/issuer/ns/ospool/ap40`

The Origin doesn't change in this step.

- **Signing keys:** keep the same keys.
- **Old tokens:** tokens issued before the switch keep working.
- **Check:** decode a new AP token (`iss` is the new URL, `kid` is one of AP40's) and transfer through a cache on
  both namespaces.

### 5. Retire the old issuer (`stages/5c-server-issuer-removed.yaml`)

Once no old-`iss` token can still be in use, make two changes:

- **Remove `https://osg-htc.org/ospool` from both exports' `IssuerUrls`.**
- **Remove `Server.IssuerUrl`.** Stage 5a, which keeps it, behaves the same, so this can be done later.

Keep `IssuerUrls` written out. Relying on the defaults (`stages/5b-defaults.yaml`) refuses every token.

## Rotating the AP's keys after step 2

Until step 5, the AP's keys are published in two places: `osg-htc.org/ospool` and the Origin's `IssuerJwks` file.
After step 5, only the file matters.

- **Adding a key:** publish it before the AP signs with it.
- **Removing a key:** remove it only after the AP stops using it.
- **No restart needed:** the Origin re-reads the file within seconds. Caches keep fetched keys for a while, so allow
  time before the AP signs with a new one.

## Results

`python3 -I verify.py` ran every stage on 2026-11-17, and all 262 checks passed. The images used:

| Image | Digest |
|---|---|
| origin:v26.0 | `sha256:fc3cdaddb1da9e3e1ee145663ce149d726524088c15808befeedc84f085208ea` |
| cache:v26.0 | `sha256:5bcd1085f4d33d07bdeb3d1280a789d787b0a3e011c92dc49311ecf0f3c08408` |
| director:v26.0 | `sha256:0bad7bec163cf4a0a28fae02dc55414ef0d49240bfbc6427a0cb10f6fbad64bd` |
| registry:v26.0 | `sha256:3df2cc317306d3edb1559dbd50de3193bc98c5cb151dba6fbb94623ed527a48a` |

All are Pelican 26.0.2.

Where each token can write and read at the Origin, and read through the Cache. Each cell is ap40 / uw-shared, and the
Origin and the Cache agreed in every stage.

| Stage | Director advertises (ap40 \| uw-shared) | Old AP token | New AP token (ES256, RS256) | ap40 embedded token | uw-shared embedded token | ap40 token with trailing-slash `iss` |
|---|---|---|---|---|---|---|
| 0 current | old \| old | ok / ok | – / – | – / – | – / – | – / – |
| 1a issuer on, nothing else | ap40-embedded/ \| uws-embedded/ | **– / –** | – / – | **– / –** | **– / –** | **– / –** |
| 1b issuer on, old pinned | old \| old | ok / ok | – / – | – / – | – / – | – / – |
| 2 AP keys published | old \| old | ok / ok | – / – | – / – | – / – | – / – |
| 3 both issuers | ap40-embedded, old \| same | ok / ok | ok / ok | ok / ok | – / – | – / – |
| 5a old issuer dropped | ap40-embedded \| ap40-embedded | – / – | ok / ok | ok / ok | – / – | – / – |
| 5c and `Server.IssuerUrl` removed | ap40-embedded \| ap40-embedded | – / – | ok / ok | ok / ok | – / – | – / – |
| 5b defaults | ap40-embedded/ \| uws-embedded/ | – / – | **– / –** | **– / –** | **– / –** | **– / –** |

- **Old AP token:** `iss=https://osg-htc.org/ospool`, signed by a simulated AP key.
- **New AP token:** `iss` is the ap40 embedded issuer, signed by a simulated AP key (one ES256, one RS256).
- **Embedded tokens:** `iss` is that export's embedded issuer, signed by the Origin's own key.
- **A trailing `/`:** marks the slash spelling of an advertised issuer.

Each stage also checked the following, and all held:

- The ap40 embedded issuer's discovery names itself without the slash.
- Its JWKS has the AP keys from stage 2 on.
- The uw-shared issuer's JWKS never has them.
- The Origin's server-wide JWKS never has them.

## The test federation

`compose.yaml` runs these containers, each from a published image:

| Container | Image | Role |
|---|---|---|
| `old-issuer` | origin (its Python) | Answers as `osg-htc.org`. It serves the federation discovery document, pointing at this federation's Director and Registry, and the `/ospool` issuer, with AP40's four real keys plus two simulated AP keys. |
| `registry` | registry:v26.0 | The federation's Registry. |
| `director` | director:v26.0 | The federation's Director. |
| `origin` | origin:v26.0 | AP40's Origin, configured with `stages/<stage>.yaml`. Hostname `ap40.uw.osg-htc.org`, XRootD `posix` storage. |
| `cache` | cache:v26.0 | A Cache. It builds its XRootD token config from the Director's advertisements. |
| `client` | origin:v26.0 | Sends the test requests (`curl`) from inside the federation. |

**Isolation and trust:**

- **Nothing reaches production.** `osg-htc.org` and `ap40.uw.osg-htc.org` are names on the compose network.
- **One test CA:** every server installs a throwaway CA as both its Pelican TLS CA and a system trust anchor, so
  Go, XRootD and curl all trust each other and the stand-in.

**How a stage runs:**

1. Restart the Origin with that stage's config.
2. Wait until the Director serves that stage's advertisement.
3. Write a seed file to each export.
4. Restart the Cache and wait until its XRootD config lists that stage's issuers.
5. Test.

**How the test differs from AP40, and why:**

- **The federation is local.** The Director, Registry and discovery are this federation's, not the OSDF's.
- **Storage prefixes are empty temporary filesystems.**
- **Caches are restarted, not waited on,** to pick up a new issuer list (see step 3).
- **Two values are copied from AP40 but don't change the result.** `Logging.Origin.Scitokens: trace` is AP40's
  value. `Origin.TokenAudience` was left at its default, which equals AP40's value.

**Not tested:**

- **Real AP40 tokens:** no real AP private keys were used.
- **Their audience:** the test tokens use the WLCG "any" audience, which XRootD accepted although the Origin's
  configured audience is `https://ap40.uw.osg-htc.org:8443`.
- **Logins and CLI token requests through the embedded issuer:** for example, which scopes a uw-shared user gets
  from the ap40 issuer.
- **Topology-defined namespaces in the real OSDF Director.**

## Running it

```sh
python3 -I verify.py                     # every stage, then tears down (about 10 minutes)
python3 -I verify.py 3-both-issuers      # chosen stages only
python3 -I verify.py up 3-both-issuers   # leave the federation running at a stage, to look around
python3 -I verify.py down
```

Needs docker and openssl. `.sim/` (ignored by git) holds the generated test CA, simulated AP keys, the Origin's and
Cache's keys, and each stage's XRootD token configs under `.sim/report/`. Delete it to start from scratch.

#!/usr/bin/env python3
"""Simulate AP40's move from Server.IssuerUrl=https://osg-htc.org/ospool to the Origin's embedded issuer,
inside a private Pelican federation built from the published v26.0 images (see compose.yaml):

  old-issuer  stand-in for https://osg-htc.org: federation discovery for this test federation, and the
              /ospool issuer publishing AP40's four real keys plus two simulated AP keys (ES256, RS256)
  registry, director
  origin      AP40's Origin (XRootD, StorageType posix) with stages/<stage>.yaml
  cache       a Cache, which builds its XRootD token config from the Director's namespace ads
  client      where test requests come from

The simulated AP keys stand in for AP40's signing keys. For every stage, the Origin is restarted with that
stage's config. The script waits until the Director serves the new advertisement, restarts the Cache so it
rebuilds its token config, and then records, for both exports (/ospool/ap40 and /ospool/uw-shared/projects):
  - the issuers the Director advertises (X-Pelican-Authorization, X-Pelican-Token-Generation)
  - the issuer sections XRootD was given on the Origin and on the Cache (saved under .sim/report/)
  - for each token, whether it can write and read at the Origin, and read through the Cache:
      old AP          iss=https://osg-htc.org/ospool, signed by a simulated AP key
      new AP ES256    iss=<ap40 embedded issuer>, signed by the simulated ES256 AP key
      new AP RS256    iss=<ap40 embedded issuer>, signed by the simulated RS256 AP key
      embedded ap40   iss=<ap40 embedded issuer>, signed by the Origin's own key
      embedded uws    iss=<uw-shared embedded issuer>, signed by the Origin's own key
      slash-iss ap40  iss=<ap40 embedded issuer> + "/", signed by the Origin's own key
  - what the embedded issuers' discovery and JWKS endpoints publish, and that no AP key reaches the
    Origin's server-wide JWKS
and compares each with the expected result.

Usage:
  python3 -I verify.py [stage ...]    run the given stages (default: all), then tear down
  python3 -I verify.py up [stage]     bring the federation up at a stage (default 0-current) and leave it
  python3 -I verify.py down           tear down
Requires docker and openssl. Nothing leaves this machine.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
COMPOSE = HERE / "compose.yaml"
SIM = HERE / ".sim"
REPORT = SIM / "report"
IMAGES = [f"hub.osg-htc.org/pelican_platform/{c}:v26.0" for c in ("origin", "cache", "director", "registry")]
ORIGIN, CACHE, CLIENT = "ap40-switch-over-origin", "ap40-switch-over-cache", "ap40-switch-over-client"
EXT = "https://ap40.uw.osg-htc.org:8444"     # AP40's Server.ExternalWebUrl
XROOTD = "https://ap40.uw.osg-htc.org:8443"  # AP40's Origin.Url
CACHE_URL = "https://cache:8443"
DIRECTOR = "https://director:8444"
AP40, UWS = "/ospool/ap40", "/ospool/uw-shared/projects"
NAMESPACES = {"ap40": AP40, "uw-shared": UWS}
STORAGE = {"ap40": "/storage/ap40", "uw-shared": "/storage/uw-shared/projects"}
OLD = "https://osg-htc.org/ospool"
NEW = f"{EXT}/api/v1.0/issuer/ns{AP40}"
NEW_UWS = f"{EXT}/api/v1.0/issuer/ns{UWS}"
NEW_SLASH, NEW_UWS_SLASH = NEW + "/", NEW_UWS + "/"
REAL_AP_JWKS = HERE / "origin" / "ap40-issuer.jwks"

TOKENS = ["old AP", "new AP ES256", "new AP RS256", "embedded ap40", "embedded uws", "slash-iss ap40"]
BOTH, NONE = {"ap40", "uw-shared"}, set()

# Expected behaviour per stage: issuers advertised per export, and where each token is accepted.
# The same acceptance is expected at the Origin and through the Cache.
STAGES = {
    "0-current": dict(
        adv={"ap40": [OLD], "uw-shared": [OLD]},
        accept={"old AP": BOTH}, ap_keys=False),
    # With the default issuer, the advertised URL keeps the export's trailing slash, but the issuer names
    # itself without one. A token with the slash spelling doesn't help under XRootD either: SciTokens looks
    # up "<iss>/.well-known/openid-configuration", i.e. ".../ap40//.well-known/...", which is a 404.
    "1a-issuer-on-only": dict(
        adv={"ap40": [NEW_SLASH], "uw-shared": [NEW_UWS_SLASH]},
        accept={}, ap_keys=False),
    "1b-issuer-on-pinned": dict(
        adv={"ap40": [OLD], "uw-shared": [OLD]},
        accept={"old AP": BOTH}, ap_keys=False),
    "2-ap-keys-published": dict(
        adv={"ap40": [OLD], "uw-shared": [OLD]},
        accept={"old AP": BOTH}, ap_keys=True),
    "3-both-issuers": dict(
        adv={"ap40": [NEW, OLD], "uw-shared": [NEW, OLD]},
        accept={"old AP": BOTH, "new AP ES256": BOTH, "new AP RS256": BOTH, "embedded ap40": BOTH},
        ap_keys=True),
    "5a-old-issuer-dropped": dict(
        adv={"ap40": [NEW], "uw-shared": [NEW]},
        accept={"new AP ES256": BOTH, "new AP RS256": BOTH, "embedded ap40": BOTH}, ap_keys=True),
    "5c-server-issuer-removed": dict(
        adv={"ap40": [NEW], "uw-shared": [NEW]},
        accept={"new AP ES256": BOTH, "new AP RS256": BOTH, "embedded ap40": BOTH}, ap_keys=True),
    "5b-defaults": dict(
        adv={"ap40": [NEW_SLASH], "uw-shared": [NEW_UWS_SLASH]},
        accept={}, ap_keys=True),
}

failures: list[str] = []


def check(ok: bool, what: str, detail: str = "") -> None:
    print(f"    {'PASS' if ok else 'FAIL'}  {what}" + (f"  (got {detail})" if detail and not ok else ""))
    if not ok:
        failures.append(what)


def run(*cmd: str, env: dict | None = None, check_rc: bool = True) -> str:
    result = subprocess.run(cmd, capture_output=True, env=env)
    if check_rc and result.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)} failed:\n{result.stderr.decode()}{result.stdout.decode()}")
    return result.stdout.decode()


def compose(stage: str, *args: str) -> None:
    run("docker", "compose", "-f", str(COMPOSE), *args, env={**os.environ, "SIM_DIR": str(SIM), "STAGE": stage})


def curl(url: str, token: str | None = None, method: str = "GET", data: str | None = None) -> tuple[int, dict, str]:
    """Request from inside the federation. Returns status, headers (lists of values) and body."""
    cmd = ["docker", "exec", CLIENT, "curl", "-s", "-D", "/tmp/headers", "-o", "/tmp/body", "-w", "%{http_code}",
           "--max-time", "30", "-X", method, url]
    if token:
        cmd += ["-H", f"Authorization: Bearer {token}"]
    if data is not None:
        cmd += ["--data-binary", data]
    status = int(run(*cmd, check_rc=False).strip() or 0)
    headers: dict[str, list[str]] = {}
    for line in run("docker", "exec", CLIENT, "cat", "/tmp/headers", check_rc=False).splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            headers.setdefault(key.strip().lower(), []).append(value.strip())
    body = run("docker", "exec", CLIENT, "cat", "/tmp/body", check_rc=False)
    return status, headers, body


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def thumbprint(members: dict) -> str:
    """RFC 7638 thumbprint: the kid Pelican assigns to a PEM key."""
    return b64url(hashlib.sha256(json.dumps(members, separators=(",", ":"), sort_keys=True).encode()).digest())


def short(url: str) -> str:
    return {OLD: "old", NEW: "ap40-embedded", NEW_UWS: "uws-embedded",
            NEW_SLASH: "ap40-embedded/", NEW_UWS_SLASH: "uws-embedded/"}.get(url, url)


# ---------------------------------------------------------------- simulation material

OPENSSL_CNF = """
[req]
distinguished_name = dn
[dn]
[v3_ca]
basicConstraints = critical, CA:TRUE
keyUsage = critical, keyCertSign, cRLSign
subjectKeyIdentifier = hash
[v3_srv]
basicConstraints = critical, CA:FALSE
keyUsage = critical, digitalSignature, keyEncipherment
extendedKeyUsage = serverAuth
subjectAltName = DNS:osg-htc.org
subjectKeyIdentifier = hash
authorityKeyIdentifier = keyid
"""


def build_sim() -> None:
    """Create .sim/ once: test CA, the stand-in's certificate and documents, the simulated AP keys and the
    IssuerJwks file. Kept between runs so the Origin's and Cache's registrations stay valid."""
    if (SIM / "ap40-issuer.jwks").exists():
        return
    for sub in ("ca", "ap-keys", "old-issuer-tls", "old-issuer-www/.well-known",
                "old-issuer-www/ospool/.well-known", "origin-keys", "cache-keys", "work"):
        (SIM / sub).mkdir(parents=True, exist_ok=True)
    work = SIM / "work"
    cnf = work / "openssl.cnf"
    cnf.write_text(OPENSSL_CNF)

    ca_key, ca_pem = SIM / "ca" / "ca.key", SIM / "ca" / "ca.pem"
    run("openssl", "req", "-x509", "-new", "-newkey", "rsa:2048", "-nodes", "-keyout", str(ca_key),
        "-out", str(ca_pem), "-days", "30", "-subj", "/CN=ap40-switch-over test CA",
        "-config", str(cnf), "-extensions", "v3_ca")
    tls = SIM / "old-issuer-tls"
    run("openssl", "req", "-new", "-newkey", "rsa:2048", "-nodes", "-keyout", str(tls / "server.key"),
        "-out", str(work / "server.csr"), "-subj", "/CN=osg-htc.org", "-config", str(cnf))
    run("openssl", "x509", "-req", "-in", str(work / "server.csr"), "-CA", str(ca_pem), "-CAkey", str(ca_key),
        "-CAserial", str(work / "ca.srl"), "-CAcreateserial", "-out", str(tls / "server.pem"), "-days", "30", "-extfile", str(cnf), "-extensions", "v3_srv")

    ec_pem, rsa_pem = SIM / "ap-keys" / "ap-ec.pem", SIM / "ap-keys" / "ap-rsa.pem"
    run("openssl", "ecparam", "-name", "prime256v1", "-genkey", "-noout", "-out", str(ec_pem))
    run("openssl", "genrsa", "-out", str(rsa_pem), "2048")
    der = subprocess.run(["openssl", "ec", "-in", str(ec_pem), "-pubout", "-outform", "DER"],
                         check=True, capture_output=True).stdout
    point = der[-65:]
    assert point[0] == 4, "unexpected EC public key encoding"
    ec = {"crv": "P-256", "kty": "EC", "x": b64url(point[1:33]), "y": b64url(point[33:])}
    modulus = run("openssl", "rsa", "-in", str(rsa_pem), "-noout", "-modulus").strip().split("=", 1)[1]
    rsa = {"e": "AQAB", "kty": "RSA", "n": b64url(bytes.fromhex(modulus))}
    sim_public = [{**ec, "kid": thumbprint(ec), "alg": "ES256", "use": "sig"},
                  {**rsa, "kid": thumbprint(rsa), "alg": "RS256", "use": "sig"}]

    jwks = {"keys": json.loads(REAL_AP_JWKS.read_text())["keys"] + sim_public}
    (SIM / "ap40-issuer.jwks").write_text(json.dumps(jwks, indent=2))
    www = SIM / "old-issuer-www"
    (www / "ospool" / ".well-known" / "issuer.jwks").write_text(json.dumps(jwks, indent=2))
    (www / "ospool" / ".well-known" / "openid-configuration").write_text(json.dumps(
        {"issuer": OLD, "jwks_uri": f"{OLD}/.well-known/issuer.jwks"}))
    (www / ".well-known" / "pelican-configuration").write_text(json.dumps({
        "director_endpoint": DIRECTOR,
        "namespace_registration_endpoint": "https://registry:8444",
        "jwks_uri": f"{DIRECTOR}/.well-known/issuer.jwks",
        "broker_endpoint": "",
    }))
    for path in SIM.rglob("*"):
        path.chmod(0o755 if path.is_dir() else 0o644)


def ap_kids() -> set[str]:
    return {k["kid"] for k in json.loads((SIM / "ap40-issuer.jwks").read_text())["keys"]}


# ---------------------------------------------------------------- waiting

def wait(what: str, condition, timeout: int = 240, interval: int = 3):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = condition()
        if last:
            return last
        time.sleep(interval)
    print(f"    (timed out after {timeout}s waiting for {what})")
    return last


def director_view(ns: str) -> tuple[int, list[str], str]:
    status, headers, _ = curl(f"{DIRECTOR}{ns}/migration-test/seed.txt")
    auth = [h.removeprefix("issuer=") for h in headers.get("x-pelican-authorization", [])]
    gen = (headers.get("x-pelican-token-generation") or [""])[0]
    return status, auth, gen.split(",")[0].removeprefix("issuer=") if gen else ""


def scitokens_cfg(container: str) -> str:
    return run("docker", "exec", container, "sh", "-c",
               "cat /run/pelican/xrootd/*/scitokens-*-generated.cfg 2>/dev/null", check_rc=False)


def issuer_sections(cfg: str) -> dict[str, str]:
    """issuer URL -> base_path, from an XRootD scitokens config."""
    out, current = {}, None
    for line in cfg.splitlines():
        m = re.match(r"\s*issuer\s*=\s*(\S+)", line)
        if m:
            current = m.group(1)
            out[current] = ""
        m = re.match(r"\s*base_path\s*=\s*(.+)", line)
        if m and current:
            out[current] = m.group(1).strip()
    return out


def bring_up(stage: str) -> None:
    build_sim()
    compose(stage, "up", "-d", "old-issuer", "registry", "director", "client")
    compose(stage, "up", "-d", "--force-recreate", "origin")
    want = STAGES[stage]["adv"]
    seen = wait("the Director to serve this stage's advertisement",
                lambda: all(director_view(ns)[1] == want[label] for label, ns in NAMESPACES.items()))
    if not seen:
        for label, ns in NAMESPACES.items():
            print(f"    Director view of {label}: {director_view(ns)}")
    for label, path in STORAGE.items():
        run("docker", "exec", ORIGIN, "sh", "-c",
            f"mkdir -p {path}/migration-test && echo seed > {path}/migration-test/seed.txt && chmod -R a+rwX {path}")
    compose(stage, "up", "-d", "--force-recreate", "cache")
    expected_issuers = {u for urls in want.values() for u in urls}
    wait("the Cache's XRootD token config to list this stage's issuers",
         lambda: expected_issuers <= set(issuer_sections(scitokens_cfg(CACHE))))
    # The Cache's XRootD also needs to be serving before the reads.
    wait("the Cache to serve", lambda: curl(f"{CACHE_URL}/")[0] not in (0,), timeout=120)


# ---------------------------------------------------------------- per-stage checks

def mint(issuer: str, subject: str, pem: str | None = None) -> str:
    cmd = ["docker", "exec", ORIGIN, "pelican-server", "origin", "token", "create",
           "--issuer", issuer, "--subject", subject, "--audience", "https://wlcg.cern.ch/jwt/v1/any",
           "--scope", "storage.read:/", "--scope", "storage.create:/", "--lifetime", "900"]
    if pem:
        cmd += ["--private-key", pem]
    return [line for line in run(*cmd).splitlines() if line.startswith("eyJ")][-1]


def run_stage(name: str) -> dict:
    want = STAGES[name]
    print(f"\n== {name} (stages/{name}.yaml) ==")
    bring_up(name)

    result = {"adv": {}, "origin": {t: set() for t in TOKENS}, "cache": {t: set() for t in TOKENS}}
    for label, ns in NAMESPACES.items():
        status, auth, gen = director_view(ns)
        result["adv"][label] = [short(u) for u in auth]
        print(f"    Director, {label}: HTTP {status}; advertised {[short(u) for u in auth]}; "
              f"token generation {short(gen) or '(none)'}")
        check(auth == want["adv"][label], f"{name}: Director advertises {label} issuers", str(auth))
        check(gen == want["adv"][label][0], f"{name}: Director token-generation issuer for {label}", gen)

    REPORT.mkdir(parents=True, exist_ok=True)
    for role, container in (("origin", ORIGIN), ("cache", CACHE)):
        cfg = scitokens_cfg(container)
        (REPORT / f"{name}-{role}-scitokens.cfg").write_text(cfg)
        sections = issuer_sections(cfg)
        for issuer, base in sections.items():
            if issuer in (OLD, NEW, NEW_UWS, NEW_SLASH, NEW_UWS_SLASH):
                print(f"    XRootD on {role}: issuer {short(issuer)} -> base_path {base}")

    tokens = {
        "old AP": mint(OLD, "old-ap", "/sim/ap-ec.pem"),
        "new AP ES256": mint(NEW, "new-ap-es256", "/sim/ap-ec.pem"),
        "new AP RS256": mint(NEW, "new-ap-rs256", "/sim/ap-rsa.pem"),
        "embedded ap40": mint(NEW, "embedded-ap40"),
        "embedded uws": mint(NEW_UWS, "embedded-uws"),
        "slash-iss ap40": mint(NEW_SLASH, "slash-iss-ap40"),
    }
    for tname, token in tokens.items():
        for label, ns in NAMESPACES.items():
            expected = label in want["accept"].get(tname, NONE)
            fname = f"{name}-{tname.replace(' ', '-')}.txt"
            put, _, _ = curl(f"{XROOTD}{ns}/migration-test/{fname}", token, "PUT", fname)
            get, _, body = curl(f"{XROOTD}{ns}/migration-test/seed.txt", token)
            ok = put in (200, 201, 204) and get == 200 and body.strip() == "seed"
            if ok:
                result["origin"][tname].add(label)
            check(ok == expected, f"{name}: {tname} token {'accepted' if expected else 'refused'} at the Origin "
                  f"on {label}", f"PUT {put}, GET {get}")
            cget, _, cbody = curl(f"{CACHE_URL}{ns}/migration-test/seed.txt", token)
            cok = cget == 200 and cbody.strip() == "seed"
            if cok:
                result["cache"][tname].add(label)
            check(cok == expected, f"{name}: {tname} token {'accepted' if expected else 'refused'} through the "
                  f"Cache on {label}", f"GET {cget}")

    kids = ap_kids()
    for label, issuer, ns in (("ap40", NEW, AP40), ("uw-shared", NEW_UWS, UWS)):
        status, _, body = curl(f"{EXT}/api/v1.0/issuer/ns{ns}/.well-known/openid-configuration")
        iss = json.loads(body).get("issuer") if status == 200 else None
        jstatus, _, jbody = curl(f"{EXT}/api/v1.0/issuer/ns{ns}/.well-known/issuer.jwks")
        published = {k["kid"] for k in json.loads(jbody).get("keys", [])} if jstatus == 200 else set()
        print(f"    {label} embedded issuer: discovery HTTP {status}" + (f" issuer={iss}" if iss else "")
              + f"; JWKS HTTP {jstatus}, {len(published)} key(s), {len(kids & published)} of them AP keys")
        if status == 200:
            check(iss == issuer, f"{name}: {label} embedded discovery names {short(issuer)}", str(iss))
        want_keys = want["ap_keys"] and label == "ap40"
        check((kids <= published) == want_keys,
              f"{name}: AP keys {'published' if want_keys else 'not published'} under the {label} embedded issuer",
              f"{len(kids & published)} of {len(kids)}")
    _, _, sbody = curl(f"{EXT}/.well-known/issuer.jwks")
    server_kids = {k["kid"] for k in json.loads(sbody).get("keys", [])}
    check(not (kids & server_kids), f"{name}: no AP key in the Origin's server-wide JWKS", str(kids & server_kids))
    return result


def main(args: list[str]) -> int:
    if args[:1] == ["down"]:
        compose("0-current", "down")
        return 0
    if args[:1] == ["up"]:
        stage = args[1] if len(args) > 1 else "0-current"
        bring_up(stage)
        print(f"Federation up at {stage}. Tear down with: python3 -I verify.py down")
        return 0

    selected = args or list(STAGES)
    unknown = [s for s in selected if s not in STAGES]
    if unknown:
        print(f"unknown stage(s) {unknown}; choose from {list(STAGES)}")
        return 2
    for image in IMAGES:
        print(run("docker", "inspect", "--format", "{{index .RepoDigests 0}}", image).strip())
    print(run("docker", "run", "--rm", "--entrypoint", "pelican-server", IMAGES[0], "--version").splitlines()[0])

    rows = []
    try:
        for name in selected:
            rows.append((name, run_stage(name)))
    finally:
        compose("0-current", "down")

    print("\nSummary: where each token is accepted, at the Origin and through the Cache (ap40 / uw-shared)")
    print(f"  {'stage':22} {'Director advertises: ap40 | uw-shared':42} " + " ".join(f"{t:16}" for t in TOKENS))
    for name, res in rows:
        adv = " | ".join(", ".join(res["adv"][label]) for label in NAMESPACES)
        for role in ("origin", "cache"):
            cells = [" / ".join("ok" if label in res[role][t] else "-" for label in NAMESPACES) for t in TOKENS]
            print(f"  {name if role == 'origin' else '':22} {(adv if role == 'origin' else ''):42} "
                  + " ".join(f"{role[0].upper()} {c:14}" for c in cells))
    print(f"\n{len(failures)} failure(s)" + (": " + "; ".join(failures) if failures else ""))
    print(f"XRootD token configs for every stage: {REPORT}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

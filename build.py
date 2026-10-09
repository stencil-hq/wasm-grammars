#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.14"
# ///
"""Build the tree-sitter grammars listed in grammars.toml as WebAssembly modules.

For every grammar: download its crates.io release, compile the grammar directory
with ``tree-sitter build --wasm --docker`` (tree-sitter-cli's pinned emscripten
image, so builds reproduce on any host), and write
``dist/<language>-<sha256[:16]>.wasm.zst``. Then write ``dist/manifest.json``,
which consumers pin, and ``dist/THIRD-PARTY-NOTICES.txt`` with the grammars'
licenses.

Usage: uv run build.py [--release TAG] [--jobs N]

Needs network access and docker (or podman).
"""

import argparse
import gzip
import hashlib
import io
import json
import platform
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import urllib.request
from compression import zstd
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DIST = ROOT / "dist"
CACHE = ROOT / ".cache"
LICENSE_FILE = re.compile(r"^(licen[cs]e|copying|notice)", re.IGNORECASE)
USER_AGENT = "stencil-hq/wasm-grammars (https://github.com/stencil-hq/wasm-grammars)"


@dataclass(frozen=True)
class Grammar:
    """One ``[[grammar]]`` entry of grammars.toml."""

    language: str
    crate: str
    version: str
    dir: str = "."


@dataclass(frozen=True)
class Built:
    """A compiled module and what the manifest records about it."""

    grammar: Grammar
    symbol: str
    file: str
    sha256: str
    size: int
    notice: str


def fetch(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=120) as response:
        return response.read()


def tree_sitter_cli(version: str) -> Path:
    """The tree-sitter-cli release binary for this host, downloaded once into .cache."""
    system = {"Linux": "linux", "Darwin": "macos", "Windows": "windows"}[
        platform.system()
    ]
    machine = {"x86_64": "x64", "amd64": "x64", "arm64": "arm64", "aarch64": "arm64"}[
        platform.machine().lower()
    ]
    binary = (
        CACHE
        / f"tree-sitter-{version}"
        / ("tree-sitter.exe" if system == "windows" else "tree-sitter")
    )
    if not binary.exists():
        url = f"https://github.com/tree-sitter/tree-sitter/releases/download/v{version}/tree-sitter-{system}-{machine}.gz"
        binary.parent.mkdir(parents=True, exist_ok=True)
        binary.write_bytes(gzip.decompress(fetch(url)))
        binary.chmod(binary.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return binary


def build(grammar: Grammar, cli: Path, scratch: Path) -> Built:
    work = scratch / grammar.language
    work.mkdir()
    archive = fetch(
        f"https://static.crates.io/crates/{grammar.crate}/{grammar.crate}-{grammar.version}.crate"
    )
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        tar.extractall(work, filter="data")
    crate_dir = work / f"{grammar.crate}-{grammar.version}"
    grammar_dir = crate_dir / grammar.dir

    parser = (grammar_dir / "src" / "parser.c").read_text(
        encoding="utf-8", errors="replace"
    )
    match = re.search(r"TSLanguage \*tree_sitter_(\w+)\(void\)", parser)
    if not match:
        raise RuntimeError(
            f"{grammar.crate} {grammar.version}: no tree_sitter_<name> export in {grammar.dir}/src/parser.c"
        )
    symbol = match[1]
    # tree-sitter-cli refuses to build without a tree-sitter.json, which older
    # crates do not ship; these are the fields it requires.
    if (
        not (grammar_dir / "tree-sitter.json").exists()
        and not (crate_dir / "tree-sitter.json").exists()
    ):
        config = {
            "grammars": [{"name": symbol, "scope": f"source.{symbol}", "path": "."}],
            "metadata": {"version": grammar.version},
        }
        (grammar_dir / "tree-sitter.json").write_text(json.dumps(config))
    # It takes the module name from src/grammar.json, which some crates omit
    # along with grammar.js; a name and an empty rule set satisfy it.
    grammar_json = grammar_dir / "src" / "grammar.json"
    if not grammar_json.exists():
        grammar_json.write_text(json.dumps({"name": symbol, "rules": {}}))

    module = work / "grammar.wasm"
    # The CLI resolves tree-sitter.json from its working directory, not from its path argument.
    result = subprocess.run(
        [cli, "build", "--wasm", "--docker", "-o", module, "."],
        cwd=grammar_dir,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"{grammar.crate} {grammar.version}: tree-sitter build failed\n{result.stderr}"
        )

    wasm = module.read_bytes()
    sha256 = hashlib.sha256(wasm).hexdigest()
    file = f"{grammar.language}-{sha256[:16]}.wasm"
    (DIST / f"{file}.zst").write_bytes(zstd.compress(wasm, level=19))
    print(
        f"{grammar.language}: {grammar.crate} {grammar.version} → {file} ({len(wasm) / 1e6:.1f} MB)",
        flush=True,
    )
    return Built(
        grammar, symbol, file, sha256, len(wasm), license_notice(grammar, crate_dir)
    )


def license_notice(grammar: Grammar, crate_dir: Path) -> str:
    """Crate heading plus its license files, or its declared license when it ships none."""
    manifest = tomllib.loads((crate_dir / "Cargo.toml").read_text())
    license = manifest["package"].get("license", "unspecified")
    files = sorted(
        path
        for path in crate_dir.iterdir()
        if path.is_file() and LICENSE_FILE.match(path.name)
    )
    body = "\n".join(path.read_text(errors="replace") for path in files)
    if not body:
        body = f"The crate ships no license file; its manifest declares {license}.\n"
    return f"{'-' * 79}\n{grammar.crate} {grammar.version} ({license})\n\n{body}"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--release",
        default="dev",
        help="release tag recorded in manifest.json (CI passes the git tag)",
    )
    parser.add_argument(
        "--jobs",
        type=int,
        default=4,
        help="grammars built in parallel (one container each)",
    )
    args = parser.parse_args()

    config = tomllib.loads((ROOT / "grammars.toml").read_text())
    grammars = [Grammar(**entry) for entry in config["grammar"]]
    cli = tree_sitter_cli(config["tree_sitter"])
    shutil.rmtree(DIST, ignore_errors=True)
    DIST.mkdir()
    with (
        tempfile.TemporaryDirectory(prefix="wasm-grammars-") as scratch,
        ThreadPoolExecutor(args.jobs) as pool,
    ):
        built = sorted(
            pool.map(lambda grammar: build(grammar, cli, Path(scratch)), grammars),
            key=lambda b: b.grammar.language,
        )

    manifest = {
        "release": args.release,
        "tree_sitter": config["tree_sitter"],
        "grammars": [
            {
                "language": b.grammar.language,
                "crate": b.grammar.crate,
                "version": b.grammar.version,
                "symbol": b.symbol,
                "file": b.file,
                "sha256": b.sha256,
                "size": b.size,
            }
            for b in built
        ],
    }
    (DIST / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    (DIST / "THIRD-PARTY-NOTICES.txt").write_text("\n".join(b.notice for b in built))
    print(f"{len(built)} grammars → {DIST.relative_to(ROOT)}/ (release {args.release})")


if __name__ == "__main__":
    sys.exit(main())

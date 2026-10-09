# wasm-grammars

tree-sitter grammars compiled to WebAssembly, for programs that load them on
demand through tree-sitter's `WasmStore` instead of linking every grammar in.
[oh-my-pi](https://github.com/can1357/oh-my-pi) links its common grammars and
downloads these for everything else.

Each release carries:

- `<language>-<sha256[:16]>.wasm.zst`: one zstd-compressed module per grammar.
- `manifest.json`: the release tag, the tree-sitter-cli version, and per grammar
  the source crate and version, the exported symbol (`tree_sitter_<symbol>`),
  the module file name, and the SHA-256 and byte size of the decompressed module.
- `THIRD-PARTY-NOTICES.txt`: the license of every grammar.

```json
{
  "release": "v1",
  "tree_sitter": "0.25.10",
  "grammars": [
    {
      "language": "verilog",
      "crate": "tree-sitter-verilog",
      "version": "1.0.3",
      "symbol": "verilog",
      "file": "verilog-46f723039630ef85.wasm",
      "sha256": "46f723039630ef85b8a2dd324cba2673207163f760eb435c800bc0f56d78bb0e",
      "size": 18135336
    }
  ]
}
```

Download a module from
`https://github.com/stencil-hq/wasm-grammars/releases/download/<release>/<file>.zst`
and check its size and SHA-256 against the manifest you pinned. The modules use
the wasm ABI of the tree-sitter-cli that built them (`tree_sitter`); load them
with a tree-sitter runtime of the same minor version.

## Building

```sh
uv run build.py            # → dist/
```

`build.py` needs Python 3.14 (uv provides it), network access, and docker or
podman: tree-sitter-cli compiles each grammar in its pinned emscripten image,
so a build reproduces byte for byte on any host.

## Adding or updating a grammar

1. Edit `grammars.toml`.
2. Open a pull request; CI builds every grammar.
3. After merging, push the next `v<N>` tag. CI publishes the release; releases
   are never modified, so consumers pinned to an older one keep working.

oh-my-pi picks up the new release with `bun run gen:grammars`.

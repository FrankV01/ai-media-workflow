"""
app.blocks — Modular workflow blocks

Each block is a self-contained processing unit that:
1. Declares its name, description, accepted inputs, and produced outputs
2. Implements a run() method with a standard signature
3. Registers itself with the block registry on import

Blocks can be anything: API calls, ffmpeg commands, AI inference, file I/O, etc.
New blocks are added as modules in this package — the registry auto-discovers them.
"""

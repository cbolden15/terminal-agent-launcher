# Security policy

## Supported versions

Security fixes are made against the latest release. Terminal Agent Launcher is currently alpha software for macOS.

## Reporting a vulnerability

Please use [GitHub private vulnerability reporting](https://github.com/cbolden15/terminal-agent-launcher/security/advisories/new). Do not open a public issue for a vulnerability that could expose local paths, launch unintended commands, or weaken permission controls.

Include the affected version, macOS version, reproduction steps, and expected impact. Remove credentials and private project paths before submitting.

## Security boundary

The server supports loopback connections only. It is not designed for LAN or internet exposure. Browser requests can select only server-discovered projects, fixed agent permission presets, and a tab or window destination. Full-bypass modes still grant the underlying agent broad local access and should be used only when the user understands that risk.

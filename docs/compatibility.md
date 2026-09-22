# Compatible devices

ReoVault talks to cameras through [`reolink-cli`](https://github.com/reolink/reolink-cli),
not a reimplementation of Reolink's protocol, so in principle any device
`reolink-cli` supports is a candidate. The table below tracks what's actually
been run against real hardware.

| Model | Discovery / connection | Archiving (search, download, verify) |
|---|---|---|
| Reolink Video Doorbell D340W | ✅ Verified | ✅ Verified. Full end-to-end archive runs. Test fixtures are derived from this device. |
| Reolink E1 Outdoor Pro | ✅ Verified | ⚠️ Not yet verified. Camera is discovered and reachable via `reolink-cli`, but a sustained archive run hasn't been confirmed. |

If you run ReoVault against a device that isn't listed here, please open an
issue (or a discussion) with the model and what worked. That's how this
table grows. See [Architecture](architecture.md#provideradapter-boundary)
for how the provider/adapter boundary keeps camera-specific behavior
isolated, and [`CLAUDE.md`](https://github.com/wiebe-vandendriessche/reovault/blob/main/CLAUDE.md)
for the project's policy on verifying protocol behavior against real
devices rather than assuming it.

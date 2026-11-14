# PS5 → Mac telemetry setup

Goal: F1 25 on the PS5 sends UDP telemetry to the Mac Mini at 60 Hz, with (almost) no lost packets.

## 1. Network

- **Use wired Ethernet for both the PS5 and the Mac if you can.** Wi-Fi works, but drops more
  packets, and dropped packets cannot be recovered.
- Both devices must be on the **same local network** (not a guest network, which usually blocks
  device-to-device traffic).
- **Give the Mac a fixed IP address.** The PS5 sends to an address you type in once. If the Mac's
  address changes, packets go nowhere. Two options:
  - *Preferred:* a DHCP reservation for the Mac in your router's settings (router-specific).
  - *Or:* on the Mac, System Settings → Network → Ethernet → Details → TCP/IP →
    Configure IPv4: "Using DHCP with manual address".

## 2. Mac

1. Install and check (see the README quick start), then run:

   ```bash
   uv run pitwall doctor
   ```

   It shows the Mac's LAN address and the exact values to enter on the PS5.
2. Start a capture:

   ```bash
   caffeinate -i uv run pitwall capture run --label setup-test
   ```

   `caffeinate -i` stops the Mac from sleeping while capturing.
3. **Firewall:** if the macOS firewall is on, the first run asks whether Python may accept incoming
   connections. Choose **Allow**. If you missed it: System Settings → Network → Firewall → Options,
   and allow the Python that uv installed.

## 3. PS5 (F1 25 → Settings → Telemetry Settings)

| Setting | Value |
|---|---|
| UDP Telemetry | On |
| UDP Broadcast Mode | Off |
| UDP IP Address | the Mac's address from `pitwall doctor` |
| UDP Port | 20777 |
| UDP Send Rate | 60Hz |
| UDP Format | **F1 25 (2025)**, *not* the 2026 Season Pack format |

Menu labels may differ slightly between game patches. That doesn't matter much, because the
capture tool checks what actually arrives: the status line shows the packet format (`fmt 2025`
expected) and the telemetry rate.

You drive the base 2025 cars, so the UDP format must be the F1 25 one. The 2026 Season Pack
format is a different packet layout, and analysis is built for the F1 25 layout first.

## 4. Check it works

Go on track (Time Trial, Monza) and watch the status line, printed every 5 seconds:

```
10:22:44    600 pkt/s · telemetry  60.0 Hz · loss 0.00% · fmt 2025 v1.xx · session …1a2b3c4d · from 192.168.1.23 · stored 21460
```

Good: telemetry ≈ 60 Hz while driving, loss below 0.5%, `fmt 2025`, a single `from` address
(the PS5). In menus and while paused, fewer or no packets arrive, which is normal.

When you stop with Ctrl+C, a checklist is printed (✓ / ! / ✗). Full details:

```bash
uv run pitwall capture stats <capture-id>
```

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| "waiting for packets…" forever | Wrong IP or port on the PS5; firewall blocking Python; devices on different networks or a guest network; UDP Telemetry off |
| `fmt` is not 2025 | UDP Format set to another year, or to the 2026 Season Pack format |
| telemetry well below 60 Hz while driving | UDP Send Rate below 60Hz |
| loss above 0.5% | Wi-Fi interference; switch to wired, or move closer to the router |
| several `from` addresses | Another device is also sending to this port; use `--allow-source <PS5 IP>` |
| "Cannot listen on UDP …:20777" | Another telemetry app or capture already uses the port; close it or use `--port` |

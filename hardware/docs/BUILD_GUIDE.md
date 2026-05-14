# AM/FM Antenna Switching Build Guide

Build a Cat-5 long-wire AM antenna and a GPIO-controlled RF relay that
auto-selects between AM (Cat-5 long-wire) and FM (Shakespeare 5120) feeds
to the single RTL-SDR dongle.

**Total cost:** ~$70–$95 depending on choices.
**Time:** ~3 hours soldering and assembly, plus printing time.
**Skill required:** comfortable through-hole soldering.

## Contents

1. [How the system fits together](#how-the-system-fits-together)
2. [Parts list](#parts-list)
3. [Schematic](#schematic-relay-driver-circuit)
4. [Antenna build](#step-1-build-the-cat-5-long-wire-antenna)
5. [Relay driver board](#step-2-build-the-relay-driver-board)
6. [Pi GPIO connection](#step-3-connect-to-the-pi-gpio)
7. [Software setup](#step-4-install-the-antenna-switch-software)
8. [Testing](#step-5-test-the-system)
9. [Case (3D-printed)](#the-case)
10. [Troubleshooting](#troubleshooting)

---

## How the system fits together

```
                    ┌────────────────────────────────────────────┐
                    │            ATTIC                           │
                    │                                            │
   Shakespeare      │                                            │
   5120 FM whip ────┼─── RG-58 coax ──┐                          │
   (roof exterior)  │                 │                          │
                    │                 ▼                          │
                    │           ┌────────────┐                   │
   Cat-5 long-wire  │           │  RF relay  │                   │
   strung across ──┐│           │ K1 = SPDT  │── short RG-58 ──┐ │
   attic           ▼│           │            │  to dongle      │ │
                   ┌┴┐          └─────▲──────┘                 │ │
                   │ │ 9:1 unun       │ control signal         ▼ │
                   │ │ matchbox       │ (5V on = AM,      ┌────────────┐
                   └┬┘                │  0V = FM)         │ Pi 5 + HAT │
                    │                 │                   │ + RTL-SDR  │
                    │                 │                   │            │
                    └─────────────────┘                   │ ── ethernet
                                  GPIO + 5V               │    out
                                                          └────────────┘
```

When the radio tunes an AM station, software pulses a GPIO pin high.
A 2N2222 transistor switches the relay coil, which throws the RF relay
from the "FM" position to the "AM" position. The SDR sees only the
selected antenna — no diplexer leakage, full isolation.

---

## Parts list

Prices and links checked as of build documentation. Substitute equivalents
freely; nothing here is brand-critical except the relay itself.

### Antenna section

| Item | Qty | Approx. cost | Source / part # | Notes |
|------|----:|------:|---|---|
| Cat 5 / 5e / 6 cable | 50–100 ft | already have | — | Any color, indoor-rated fine; just needs 4 twisted pairs |
| 9:1 unun balun | 1 | $20 | MFJ-16010 ([Amazon B003JNAR9I](https://www.amazon.com/s?k=MFJ-16010)) or Nooelec 9:1 balun ($14, [Amazon B07R7KZ7J9](https://www.amazon.com/s?k=Nooelec+9%3A1+balun)) | Either works; MFJ is sturdier |
| Insulator (end of wire) | 2 | $3 | Any "antenna insulator" or even a plastic zip-tie loop | Just keeps the wire end from grounding |
| Coax for the AM run (balun → relay) | 6–10 ft | $8 | RG-58 with male SMA on one end ([Amazon B07PW5JV3F](https://www.amazon.com/s?k=RG58+SMA+cable+10ft)) | Short run; loss is irrelevant |

### Relay + control electronics

| Item | Qty | Approx. cost | Source / part # | Notes |
|------|----:|------:|---|---|
| **SPDT RF relay** (the critical part) | 1 | varies | See *Relay choice* below | |
| 2N2222 NPN transistor (TO-92) | 2 | $3 | Any electronics supplier; [Amazon assortment B01N4FP08U](https://www.amazon.com/s?k=2N2222+TO-92) | 1 is the driver, 1 is a spare |
| 1N4148 small-signal diode | 1 | $2 | [Amazon B07Z5W4HHV](https://www.amazon.com/s?k=1N4148+pack) | Flyback diode across the relay coil |
| 1 kΩ resistor (1/4 W) | 2 | $1 | Any resistor assortment | Base resistor for the transistor |
| 10 kΩ resistor (1/4 W) | 1 | — | Same assortment | Pull-down on the GPIO pin |
| Female–female DuPont jumper wires | 1 set | $5 | [Amazon B07GD2BWPY](https://www.amazon.com/s?k=Dupont+jumper+wire+kit) | For GPIO to driver board |
| 2-pin male header (0.1"/2.54mm) | 1 | $2 | Common in any kit | Output to relay coil |
| Small protoboard (5×7 cm) | 1 | $5 | [Amazon B07ZV8FFNV](https://www.amazon.com/s?k=prototype+pcb+5x7) | The driver board |
| 22 AWG hookup wire | small spool | $5 | Any | For point-to-point on protoboard |
| SMA panel-mount jack | 1 | $6 | [Amazon B07R5KTWZ8](https://www.amazon.com/s?k=SMA+panel+mount+jack) | For the case opening |
| Short SMA male-to-male jumpers (4 in) | 2 | $8 | [Amazon B07TVKK8KH](https://www.amazon.com/s?k=SMA+SMA+jumper+10cm) | Relay ports → panel jacks → cables |

### Relay choice — the important call

The relay is the single most important component for signal quality. Three
practical options at different price/performance points:

**Option A — Mini-Circuits ZASW-2-50DR+ (best quality, ~$80)**
- DC–3 GHz, <0.7 dB insertion loss, >75 dB isolation
- 5 V coil, SMA connectors built in
- Order direct from minicircuits.com (search "ZASW-2-50DR+")
- This is what serious RF labs use; almost no hobbyist need exceeds it

**Option B — Generic SMA SPDT RF relay (~$25, the practical pick)**
- 12 V coil, often DC–6 GHz spec, SMA connectors built in
- [Amazon search "SMA SPDT RF relay 12V"](https://www.amazon.com/s?k=SMA+SPDT+RF+relay+12V) — multiple sellers, expect 1–3 dB insertion loss
- Requires a small DC-DC boost or a 12 V rail (Pi PoE provides 5 V only, so we'd need a $4 5V→12V module like [Amazon B07F87M5HZ](https://www.amazon.com/s?k=MT3608+5V+12V+boost))

**Option C — Cheap general-purpose 5 V relay (~$5, NOT recommended)**
- e.g. SRD-05VDC-SL-C on hobby relay boards
- These are mechanical relays designed for DC up to ~30 V or audio AC
- Will *work* below ~30 MHz (so OK for AM!) but degrade significantly
  above 100 MHz (bad for FM)
- Worth knowing about but skip it for a permanent install

**My recommendation: Option B.** Spend $25 on a real RF relay, $4 on a
5→12 V boost converter, total $29 in the switching section. This gets
you 95% of Option A's performance at 1/3 the price. The build below
assumes Option B; if you choose A, the 5→12 V boost is unnecessary and
the driver transistor is wired to switch 5 V instead of 12 V.

### 3D-printed case

| Item | Qty | Notes |
|------|----:|---|
| PETG or PLA filament | ~80 g | PETG preferred for attic temperatures (PLA softens at 60 °C and attics can hit 50+ °C in summer) |
| M2.5 × 12 mm screws | 4 | Mounts Pi to case bottom |
| M2.5 × 6 mm screws | 4 | Top to bottom of case |
| M3 × 8 mm screws + nuts | 4 | Mounts relay board to case top |

### Tools

- Soldering iron (any 25 W+ adjustable iron; 30–40 °C lower than usual for the SMA jack body)
- Solder (60/40 leaded is easier than lead-free if you have it)
- Wire stripper for 22 AWG and Cat 5 conductors
- Small flush-cutters
- Multimeter (for continuity and verifying GPIO voltages)
- Heat-shrink tubing in a few sizes
- Optional but very useful: helping-hands clamp or PCB vise

---

## Schematic: relay driver circuit

Standard low-side NPN switch with a flyback diode across the relay coil
to absorb the inductive kickback when the coil de-energizes.

```
                +12V  (from boost converter or external supply)
                  │
                  │       ┌──────────────┐
                  ├───────┤ Relay coil + │
                  │       │              │  K1 (RF relay)
                  │       │ Relay coil - ├──┐
                  │       └──────────────┘  │
                  │            ▲            │
                 ─┴─  D1       │            │
                 ▲ ▲  1N4148   │ collector  │
                 │ │ cathode   │            │
                 └─┴─ at +12V  │            │
                               │            │
                              [C] 2N2222   │
                            [B]    [E]     │
                               │            │
                  ┌────R1──────┘            │
                  │  1kΩ                    │
                  │                         │
                  │              R2         │
        GPIO 17 ──┤            10kΩ         │
                  │              │          │
                  │              │          │
                  │              │          │
                  └──────────────┴──────────┴──── GND (shared with Pi)

Logic: GPIO 17 high (3.3 V) → R1 saturates Q1 → coil energizes → relay
flips to AM position. GPIO 17 low → coil de-energizes → relay returns
to FM position (the normally-closed contact). R2 pulls the base down
when GPIO is high-impedance (during Pi boot or shutdown).

The flyback diode D1 is wired BACKWARDS (cathode to +12V): it does
nothing during normal operation but conducts the coil's collapse-
field current when the transistor switches off, preventing inductive
spikes from frying Q1.
```

### Why this design

- **Normally-closed = FM:** if the Pi is off, hangs, or the GPIO control fails, the relay defaults to FM. FM listening keeps working even if everything software-side is broken.
- **NPN low-side switch:** simplest possible driver, well-understood failure modes.
- **R2 pull-down:** during Pi boot the GPIO pins are in indeterminate state. Without the pull-down, the relay could click randomly. With it, GPIO floats safely low until our software claims the pin.
- **Separate +12V rail:** the relay coil draws ~30 mA at 12 V. Tapping that off the Pi's 5 V GPIO would work for Option A's 5 V relay, but with Option B's 12 V relay we need the boost converter. We power both from the same source as the Pi (the PoE HAT's 5 V output via a side tap), keeping everything on one cable.

---

## Step 1: Build the Cat-5 long-wire antenna

### Plan the run

Find the longest straight (or gently bending) run you can manage in your
attic. **Longer is always better for AM** — every doubling of length adds
~6 dB. Useful guidelines:

- **Minimum useful length:** 30 ft. Below this you're not gaining much over the SDR's stock whip.
- **Sweet spot:** 50–75 ft. Strong improvement, easy to route in most attics.
- **Maximum return on length:** 150 ft. Beyond this, gain levels off and you start picking up more local RF noise.
- **Don't worry about height** at AM wavelengths; ground-level antennas work fine.
- **Avoid running parallel to AC wiring** for >5 ft if you can; it picks up 60 Hz harmonics and switching-supply noise.
- **Avoid metal HVAC ducts and aluminum-foil insulation backing.** They'll absorb signal.

### Strip and prepare the cable

You'll have one Cat 5 run, two ends:

**Far end** (the end away from the SDR, the radiating tip):

1. Strip 4 inches of outer jacket
2. Untwist all four pairs into 8 individual conductors
3. Strip 1/2 inch of insulation off each conductor
4. Twist all 8 conductors together into a single bundle, then solder them
5. Apply heat-shrink over the joint
6. Tie the cable end to a non-conductive support (insulator, plastic zip tie, fishing line) — never let the conductors touch a grounded surface

**Near end** (the end that feeds the balun):

1. Strip 4 inches of outer jacket
2. Separate the 4 pairs into two groups:
   - **Antenna leads** (will go to balun "antenna" terminal): all 8 conductors of orange+green+blue+brown pairs joined together — wait, that's all of them. Use 4 conductors for this. Pick orange (both wires of the orange pair) and green (both wires of the green pair). That's 4 conductors joined together = your antenna lead.
   - **Counterpoise leads** (will go to balun "ground" terminal): blue pair + brown pair = 4 conductors joined together = your counterpoise lead.
3. Strip 1/2" of each, twist the 4 conductors of each group together, solder, heat-shrink

You now have two stiff "leads" coming out of your Cat-5 cable end — one
antenna, one counterpoise.

### Wire the balun

The MFJ-16010 and Nooelec 9:1 balun both have screw terminals on the
antenna side and an SO-239 or BNC on the radio side.

1. Strip antenna lead end, slide under "ANTENNA" screw terminal, tighten
2. Strip counterpoise lead end, slide under "GROUND" screw terminal, tighten
3. Use ohmmeter to confirm: no continuity between ANTENNA and GROUND terminals (should be 100s of Ω through the transformer, but no direct short)
4. Connect a 6–10 ft length of RG-58 coax with SMA male on the far end to the radio side of the balun (use an SO-239→SMA or BNC→SMA adapter if needed; some baluns ship with one)

### Test the antenna before installing permanently

Before climbing into the attic to mount everything, do a bench test:

1. Lay the Cat 5 wire out on the floor (any geometry — bent, coiled is fine for this test, it just needs to be ~30 ft+ long)
2. Connect the balun, RG-58, and SMA male to your SDR dongle directly (bypassing the future relay for now)
3. Run an AM scan: `sudo systemctl start sdr-am-scan`
4. Check results: `cat /var/lib/sdr-streams/stations_am.json | jq '.stations[0:10]'`

You should see 10–25+ stations in this scan vs. the 3 stations the TV-antenna
setup currently finds. If you don't, something's wrong with the antenna
assembly — most likely a bad solder joint or the balun is wired backwards.

Once the bench test works, install the wire permanently in the attic.

---

## Step 2: Build the relay driver board

### Layout

Use the 5×7 cm protoboard. Suggested layout (top-down view):

```
        +12V    GND
          │      │
          ┃      ┃                                  ─── 12V boost output
          ┃      ┃   ┌────────┐
          ┃      ┃   │  K1    │
          ┃      ┃   │ relay  │   ← if using a separate relay module,
          ┃ ┌────╂───┤ coil+  │     mount it next to the driver board
          ┃ │    ┃   │ coil-  │     and run wires; if SMA-built-in, the
          ┃ │   ─┷─  │        │     relay is on its own
          ┃ │   ▲▲   └────────┘
          ┃ │ D1▲▲
          ┃ │  ─┃─
          ┃ │   │ ┌─[2N2222]─┐
          ┃ │   │ │ collector │
          ┃ │   └─┤ base      ├─R1(1kΩ)─── from Pi GPIO 17
          ┃ │     │ emitter   │
          ┃ └─────┴───┬───────┘
          ┃           │
          ┃           ├──── R2(10kΩ) ─── from Pi GND
          ┃           │
          ┃           └──── shared GND (board + Pi)

GPIO header (right edge of board):
  ┌────────────┐
  │ ● GPIO 17  │ ← signal in
  │ ● GND      │ ← shared with Pi
  └────────────┘
```

### Soldering sequence (recommended order)

1. Solder the **2N2222 transistor first** (TO-92, flat side faces a specific direction per the schematic — emitter on right when flat side faces you). Don't apply heat for more than ~3 seconds at a time.
2. Solder the **1 kΩ resistor (R1)** between the transistor base and the GPIO-17 input pin
3. Solder the **10 kΩ resistor (R2)** between the transistor base and ground
4. Solder the **1N4148 diode (D1)** across the relay coil terminals — **cathode (the end with the black band) goes to +12V**, anode goes to the transistor collector. Backwards orientation = no protection; verify before powering on.
5. Solder the **2-pin header** at the relay coil output
6. Solder the **input pin header** (2 pins: GPIO 17 input, GND)
7. Solder a **+12V input wire** and a **GND wire** that will go to the boost converter
8. Use a multimeter in continuity mode to verify:
   - Base to GPIO-17 input: through R1 (~1 kΩ)
   - Base to GND: through R2 (~10 kΩ)
   - Collector to coil terminal: short (no resistance)
   - Coil + to +12V rail: short
   - Diode polarity: continuity from anode to cathode in one direction only

### Bench-test the driver before connecting to the Pi

This is mandatory. A miswired transistor or a backwards diode can damage
the GPIO pin if you skip this step.

1. With nothing connected to the Pi side, apply +12V to the board's power rail and GND to ground. The relay should **not click** yet (GPIO 17 input is floating, pulled down by R2).
2. Connect the GPIO-17 input briefly to +3.3V (use a bench supply or a battery). The relay should **click immediately**.
3. Release. Relay should **click back** (back to FM position).
4. Confirm both positions are stable — touching the relay shouldn't make it chatter.

If the relay clicks but doesn't return, you forgot R2 (the pull-down).
If it doesn't click at all, R1 is too high, the transistor is in
backwards, or the relay is rated for a different voltage than you applied.

---

## Step 3: Connect to the Pi GPIO

The UCTRONICS U627803 HAT exposes 36 of 40 GPIO pins through its cutout.
You need three pins:

| Pi Pin (physical) | Function | Wire to |
|------:|---|---|
| Pin 4 (5V) | Power for boost converter input | "+5V" on the boost board |
| Pin 11 (GPIO 17) | Relay control signal | "GPIO 17" input on driver board |
| Pin 6 (GND) | Shared ground | "GND" on driver board AND "GND" on boost board |

Reference: looking at the Pi from above with the USB ports on the right
and Ethernet/HDMI on the left, pin 1 is at the top-left of the GPIO
header (the corner farthest from the USB ports). Pins 1, 3, 5, 7, 9, 11
are the left column; 2, 4, 6, 8, 10, 12 are the right column.

```
            ┌────────────────────────────────┐
            │ 1  ● ● 2  (5V)                 │
            │ 3  ● ● 4  ← +5V to boost       │
            │ 5  ● ● 6  ← GND                │
            │ 7  ● ● 8                       │
            │ 9  ● ● 10                      │
            │ 11 ● ● 12                      │
            │  ↑                             │
            │  GPIO 17 to driver             │
            │                                │
            │ ... (remaining 28 pins unused) │
            └────────────────────────────────┘
```

Use **female-to-female DuPont jumpers** for these three connections. They
push onto the Pi's exposed GPIO pins and onto the male headers you
soldered to the driver board. No soldering required at the Pi side.

### Boost converter wiring (Option B only)

The MT3608 5→12V boost converter is a tiny PCB with 4 solder pads:

- IN+ : connect to Pi pin 4 (+5V)
- IN- : connect to Pi pin 6 (GND)
- OUT+ : connect to driver board's "+12V"
- OUT- : connect to driver board's "GND"

Before connecting to the driver board, **adjust the output voltage**:

1. Power the boost board with 5 V to its IN pads (use a USB power supply with bare wires, or temporarily connect to Pi pins 4 and 6)
2. Measure between OUT+ and OUT- with a multimeter
3. Turn the trimpot screw slowly until you read 12.0 V (it's multi-turn — could be 5–20 turns)
4. Disconnect, then wire it into the relay driver board permanently

The MT3608 can over-volt; if the trimpot goes wild and you get 20+ V,
turn it back down before connecting it to anything else.

---

## Step 4: Install the antenna-switch software

The actual code changes will be made by Claude Code on the Pi, guided by
this conversation's context plus `CLAUDE.md`. But for reference, here's
what's involved:

### Python module: `antenna_switch.py`

A small module that uses `gpiozero` to toggle the GPIO pin. Key behaviors:

- Reads `ANTENNA_GPIO_PIN=` from an env file (defaults to 17)
- If pin is unset or `gpiozero` import fails: function returns silently (no-op). Lets the code be deployed before hardware is ready.
- Caches the last-set band in a state file so we don't toggle on every Flask request if the band hasn't changed (relays don't love rapid switching).
- Exposes `set_band("fm" | "am")`. HD mode uses the FM antenna.

### Hooks in app.py

In `tune()` and `api_tune()`, before `write_env()`:

```python
import antenna_switch
antenna_switch.set_band("am" if band == "am" else "fm")
```

In `fm_scan` / `am_scan` services: add an `ExecStartPre` that runs a
small one-shot script which sets the right band.

### Install

```bash
sudo apt install -y python3-gpiozero
echo "ANTENNA_GPIO_PIN=17" | sudo tee -a /etc/sdr-streams/active.env
```

When the actual code lands, Claude Code will install it via `deploy.sh`
and restart `sdr-tuner`. After that, every tune operation auto-selects
the right antenna.

---

## Step 5: Test the system

Once everything is wired:

1. **Power on the Pi** and confirm normal operation: web UI loads, FM stations tune. Listen to a known-good FM station to verify the relay's default position (FM) is correct.
2. **From a terminal,** manually toggle the GPIO to verify the relay clicks:
   ```bash
   sudo apt install -y python3-gpiozero
   sudo -u radio python3 -c "
   from gpiozero import LED
   from time import sleep
   led = LED(17)
   for i in range(3):
       led.on();  print('AM'); sleep(2)
       led.off(); print('FM'); sleep(2)
   "
   ```
   You should hear the relay click 6 times.
3. **Tune the SDR** to a known-strong AM station while the relay is in AM position. The audio should come through clean. Toggle to FM (it should go to static, since AM frequencies don't exist in FM band).
4. **Then install the software hooks** and confirm that the radio UI's tune button auto-toggles the relay correctly.

---

## The case

See `case/case.scad` for the OpenSCAD source. Render to STL with the
provided settings, or modify dimensions as needed.

**Important:** I'd recommend printing the **standoff test piece** first
(see `case/test_fit.scad`). It's a small ~40×40mm piece that just
verifies the M2.5 hole spacing matches your Pi 5. ~10 minutes to print.
If your Pi seats correctly with the four M2.5 screws, the full case is
safe to print. If it doesn't, adjust `pi_hole_spacing_x/y` in the SCAD
and re-print the test piece.

Two parts:

**Bottom** (~6 hours print): houses the Pi 5 + UCTRONICS HAT. Cutouts
for HDMI, USB, Ethernet, microSD, and a slot above the GPIO header for
ribbon to pass up to the top piece.

**Top** (~3 hours print): mounts the relay driver board and the boost
converter. Has two SMA panel cutouts (for the AM and FM antenna feeds)
and one SMA cutout for the output to the SDR. The boards mount via
four M3 screws into printed standoffs.

The two parts bolt together with 4 M2.5×6mm screws at the corners.

---

## Troubleshooting

**Relay doesn't click when GPIO 17 toggles.**
- Multimeter the +12V rail at the relay coil terminals. If <11V, the boost converter is set wrong or undersized.
- Multimeter GPIO 17 with the test script: it should go from 0V to 3.3V. If it stays at 0V, the GPIO pin is wrong (re-check pin 11) or the Pi's pin is dead.
- The transistor is in backwards. The TO-92 flat side faces a specific direction; the E-B-C order matters. 2N2222 vs 2N3904 vs PN2222 all have *slightly different* pinouts despite the similar names — verify against the part datasheet.

**Relay clicks but AM still sounds bad.**
- Likely the antenna, not the relay. Disconnect the FM whip and connect the AM Cat-5 directly to the SDR (bypass the relay). If it sounds the same, the antenna's the problem.
- Bad solder joint on Cat-5 conductors at the far end. Cut off 2 inches and re-do the bundle.
- 9:1 balun wired backwards (antenna and ground swapped). Swap them.

**FM works fine before relay is added, gets noisier after.**
- The relay is adding insertion loss (1–3 dB on the cheap relays). Confirm by bypassing the relay.
- The relay's RF performance degrades above its rated frequency. Most "RF" relays sold as "DC–3 GHz" are honest; some sold as "DC–6 GHz" actually start rolling off at 500 MHz.

**Random clicking when the Pi boots.**
- The R2 pull-down isn't in. Without it, GPIO 17 floats during the brief window between kernel boot and the userspace daemon claiming the pin.

**The Pi's PoE works but the boost converter doesn't deliver 12V.**
- Most likely: the PoE HAT can only deliver 5V at ~4.5A *to the Pi*, not 4.5A to external loads. The relay coil pulls ~30 mA so this should be fine, but the boost converter is inefficient at low loads — try a different MT3608 or use a small linear regulator instead.

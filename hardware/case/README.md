# Case files

3D-printable case for Pi 5 + UCTRONICS U627803 PoE HAT + relay driver
board.

## Files

- **`test_fit.scad`** — small test plate with just the Pi 5 mounting
  holes. **PRINT THIS FIRST** (10 minutes). Verifies that the M2.5 hole
  pattern in `case.scad` lines up correctly with your board.
- **`case.scad`** — full two-piece case. Render with `bottom_only=true`
  for the bottom, `top_only=true` for the top, or with both false to
  preview the assembly.

## Honest disclaimer about the case design

The Pi 5 dimensions in this SCAD are from Raspberry Pi's published
mechanical drawing and should be exact. The standoffs, mounting holes,
and board outline are verified.

**What's NOT verified:** the relay board, RF relay module, and boost
converter dimensions are educated guesses based on typical
Amazon-grade hobby parts. The specific parts you order may be a few
millimeters different. The case has 5mm of margin in every direction so
small differences shouldn't matter, but if your relay is much larger
than ~30×25×15 mm, you may need to bump `top_height` or relocate the
panel SMA cutouts.

**Recommended print order:**

1. `test_fit.scad` — confirm Pi mounting holes are correct
2. The **bottom** of `case.scad` — confirm Pi + HAT fit, all connectors
   reach their cutouts, microSD card is removable
3. The **top** of `case.scad` — confirm relay board mounts, SMA jacks
   thread in cleanly

If anything's wrong after step 2 or 3, fix the parameter at the top of
`case.scad` and re-print only the affected half.

## Rendering to STL

In OpenSCAD:

1. Open `case.scad`
2. To render the **bottom**: leave `bottom_only = true; top_only = false;`
3. Press F6 to render (takes 1-2 minutes for full resolution)
4. File → Export → Export as STL
5. To render the **top**: change to `top_only = true;` and repeat

Command-line alternative:

```bash
openscad -D 'bottom_only=true; top_only=false;' -o case_bottom.stl case.scad
openscad -D 'bottom_only=false; top_only=true;' -o case_top.stl case.scad
openscad -o test_fit.stl test_fit.scad
```

## Print settings

- **Material:** PETG (recommended for attic temperatures) or PLA+
  (acceptable if your attic stays below ~50°C)
- **Layer height:** 0.2 mm
- **Infill:** 20% (more if you want it to feel sturdy)
- **Supports:** under SMA cutouts only (overhang)
- **Brim:** if your printer has bed-adhesion issues
- **Estimated time:** ~6 hours bottom, ~3 hours top
- **Filament needed:** ~80 g total

## After printing

1. Test-fit the Pi to the bottom standoffs with all four M2.5×12mm screws **before installing the UCTRONICS HAT.** Make sure microSD card pops in/out cleanly through the bottom-edge cutout.
2. Install the HAT and screw it down to the secondary standoffs the HAT brought.
3. Solder the relay driver board and bench-test it (see BUILD_GUIDE.md step 2).
4. Mount the relay driver board into the top piece with 4 M3×8mm screws.
5. Wire the SMA panel-mount jacks to the relay's RF ports using the short SMA-SMA jumpers.
6. Connect the relay-board GPIO inputs to the Pi via DuPont jumpers through the slot in the case.
7. Bolt the top and bottom together with 4 M2.5×6mm screws at the corners.

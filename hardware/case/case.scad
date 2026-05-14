// case.scad
//
// Two-piece case for Raspberry Pi 5 + UCTRONICS U627803 PoE HAT
// + relay driver board + RF relay + boost converter.
//
// Print both modules below; render with `bottom_only=true` for the lower
// half, `top_only=true` for the upper half. Bambu/Prusa: 0.2mm layer
// height, PETG, 20% infill, supports under cutouts only.
//
// MEASURED PARAMETERS:
// - Pi 5 board: 85mm x 56mm (verified, source: rpi.com mechanical drawing)
// - Pi 5 mounting holes: 58mm x 49mm M2.5 spacing, 3.5mm inset
// - UCTRONICS U627803: 65mm x 58mm, mounts on Pi's standoffs, sits ~16mm
//   above the Pi
//
// GUESSED PARAMETERS (verify before final print):
// - Relay board area: 50mm x 70mm (the 5x7 protoboard from parts list)
// - Boost converter: ~25mm x 17mm (typical MT3608)
// - SPDT RF relay with SMA connectors: ~30mm x 25mm x 15mm (typical
//   for Amazon-grade Option B parts; YOUR relay may be different)
//
// If your relay or boost converter is different sizes, edit the
// relay_*, boost_*, and panel_sma_* variables below.
//
// ALSO: print test_fit.scad first to verify the Pi mounting holes
// land correctly. If those holes are off, fix this file's pi_hole_*
// parameters to match what worked in the test piece.

$fn = 64;

// ──────────────────────────────────────────────────────────
// PARAMETERS — adjust to match your actual hardware
// ──────────────────────────────────────────────────────────

// What to render
bottom_only = true;     // true: print bottom; false: print top
top_only = false;       // overrides bottom_only

// Pi 5 + PoE HAT
pi_board_x       = 85.0;
pi_board_y       = 56.0;
pi_hole_spacing_x = 58.0;
pi_hole_spacing_y = 49.0;
pi_hole_inset     = 3.5;
pi_board_thickness = 1.6;
pi_below_clearance = 5.0;  // clearance under board for through-hole stubs
poe_hat_height   = 16.0;   // top of HAT above Pi board surface
m2_5_clearance_dia = 2.8;
m2_5_standoff_outer_dia = 5.5;

// Relay driver protoboard (5x7cm)
relay_board_x = 50.0;
relay_board_y = 70.0;
relay_board_inset = 5.0;   // margin from edge of top piece
relay_hole_spacing_x = 44.0;  // typical M3 spacing on 5x7 protoboard
relay_hole_spacing_y = 64.0;
m3_clearance_dia = 3.4;
m3_standoff_outer_dia = 6.0;

// Panel-mount SMA jacks
panel_sma_dia = 6.5;        // typical SMA panel-mount through-hole
panel_sma_nut_dia = 10.0;   // hex nut OD (just informational)
sma_z_offset = 12.0;        // height of SMA cutout center above case floor

// Case sizing
wall = 2.4;                 // wall thickness
floor_thickness = 2.5;
bottom_height = pi_below_clearance + pi_board_thickness + poe_hat_height + 3;
top_height = 22.0;          // tall enough for relay + boost + clearance

// Compute overall case footprint — must contain Pi + some routing space
case_x = pi_board_x + 12;   // 6mm margin each side
case_y = pi_board_y + 12;
case_corner_r = 4;

// Pi cutouts (offsets from board origin, looking at Pi from above with
// ethernet on the right edge, USB on the right edge below ethernet,
// HDMI ports on left edge)
//
// CORRECTION: looking at the Pi5 with the GPIO header at the top of
// your view, ethernet+USB are on the BOTTOM edge, microSD slot is
// on the BOTTOM-LEFT, HDMI is on the LEFT edge.
//
// We orient the case so the SMA antenna jacks are on the TOP edge
// (where the GPIO is) and the network/USB are on one of the SIDE
// edges accessible from outside.

// Cutout positions (in case-local coordinates, x = right, y = up)
// These assume the Pi sits centered in the case
cutout_ethernet_w = 16.5;
cutout_ethernet_h = 14.0;
cutout_usb_w = 14.0;
cutout_usb_h = 16.5;
cutout_hdmi_w = 7.5;
cutout_hdmi_h = 5.5;
cutout_microsd_w = 13.0;
cutout_microsd_h = 3.0;
cutout_usb_c_w = 9.0;
cutout_usb_c_h = 4.0;

// Pi connector offsets (X = along long axis, measured from board center)
//   Ethernet center: x = +24mm from board center, y = +14mm
//   USB cluster:     x = +25mm from board center, y = -14mm
//   HDMI ports:      y = -10mm from board center, x = -27.5 to -34.5
//   microSD:         x = -42mm from board center, y = 0
//   USB-C (power):   x = -35.5mm from board center, y = +14mm
// (Approximate; based on Pi 5 mechanical drawing)

// ──────────────────────────────────────────────────────────
// MODULES
// ──────────────────────────────────────────────────────────

module rounded_box(x, y, z, r) {
    hull() {
        for (xi = [r, x - r])
            for (yi = [r, y - r])
                translate([xi, yi, 0])
                    cylinder(r = r, h = z);
    }
}

// Standoff with concentric clearance hole
module standoff(height, outer_dia, hole_dia) {
    difference() {
        cylinder(d = outer_dia, h = height);
        translate([0, 0, -0.1])
            cylinder(d = hole_dia, h = height + 0.2);
    }
}

// ── Bottom piece ───────────────────────────────────────────
module bottom() {
    difference() {
        // Outer shell
        rounded_box(case_x, case_y, bottom_height, case_corner_r);

        // Hollow interior
        translate([wall, wall, floor_thickness])
            rounded_box(case_x - 2*wall, case_y - 2*wall,
                        bottom_height, case_corner_r - 1);

        // === Cutouts for Pi connectors ===
        // Orient: Pi origin at center of case footprint, so case-coord
        // for board center is [case_x/2, case_y/2]
        pi_cx = case_x/2;
        pi_cy = case_y/2;
        pi_z = floor_thickness + pi_below_clearance;

        // Ethernet (right side of case, near top edge)
        translate([case_x - wall - 0.1,
                   pi_cy + 14 - cutout_ethernet_w/2,
                   pi_z + 1])
            cube([wall + 0.5, cutout_ethernet_w, cutout_ethernet_h]);

        // USB cluster (right side of case, near bottom edge)
        translate([case_x - wall - 0.1,
                   pi_cy - 14 - cutout_usb_w/2,
                   pi_z + 1])
            cube([wall + 0.5, cutout_usb_w, cutout_usb_h]);

        // HDMI ports (left side of case)
        for (offset = [-34.5, -27.5])
            translate([-0.1, pi_cy + offset + pi_cx - cutout_hdmi_w/2,
                       pi_z + 1])
                cube([wall + 0.5, cutout_hdmi_w, cutout_hdmi_h]);

        // USB-C power (left side, opposite end from HDMI)
        translate([-0.1, pi_cy + 14 - cutout_usb_c_w/2, pi_z + 1])
            cube([wall + 0.5, cutout_usb_c_w, cutout_usb_c_h]);

        // microSD card slot (bottom edge of case)
        translate([pi_cx - 42 + pi_cx - cutout_microsd_w/2,
                   -0.1, pi_z - 2])
            cube([cutout_microsd_w, wall + 0.5, cutout_microsd_h + 4]);

        // Slot in top wall for GPIO ribbon to pass through to upper half
        translate([pi_cx - 15, case_y - wall - 0.1, pi_z + poe_hat_height - 4])
            cube([30, wall + 0.5, 5]);

        // Top-piece mounting screw holes (4 corners)
        screw_inset = 4;
        for (x = [screw_inset, case_x - screw_inset])
            for (y = [screw_inset, case_y - screw_inset])
                translate([x, y, bottom_height - 6])
                    cylinder(d = m2_5_clearance_dia, h = 7);
    }

    // === Pi standoffs ===
    pi_cx = case_x/2;
    pi_cy = case_y/2;
    standoff_h = floor_thickness + pi_below_clearance;
    for (x = [-pi_hole_spacing_x/2, pi_hole_spacing_x/2])
        for (y = [-pi_hole_spacing_y/2, pi_hole_spacing_y/2])
            translate([pi_cx + x, pi_cy + y, 0])
                standoff(standoff_h,
                         m2_5_standoff_outer_dia,
                         m2_5_clearance_dia);
}

// ── Top piece ──────────────────────────────────────────────
module top() {
    difference() {
        // Outer shell
        rounded_box(case_x, case_y, top_height, case_corner_r);

        // Hollow interior
        translate([wall, wall, 0])
            rounded_box(case_x - 2*wall, case_y - 2*wall,
                        top_height - floor_thickness, case_corner_r - 1);

        // === SMA panel-mount cutouts ===
        // Three jacks on the top edge (case rear): AM in, FM in, SDR out
        // Spaced to fit hex nut clearance
        for (offset = [-15, 0, 15])
            translate([case_x/2 + offset, case_y - wall - 0.1, sma_z_offset])
                rotate([-90, 0, 0])
                    cylinder(d = panel_sma_dia, h = wall + 0.5);

        // Mounting hole for relay driver board (4 corner holes)
        // Position the board in the lower half of the top piece
        rb_cx = case_x/2;
        rb_cy = case_y/2 - 5;
        for (x = [-relay_hole_spacing_x/2, relay_hole_spacing_x/2])
            for (y = [-relay_hole_spacing_y/2, relay_hole_spacing_y/2])
                translate([rb_cx + x, rb_cy + y, -0.1])
                    cylinder(d = m3_clearance_dia, h = floor_thickness + 0.2);

        // Bottom-piece mounting screw holes (matching the bottom)
        screw_inset = 4;
        for (x = [screw_inset, case_x - screw_inset])
            for (y = [screw_inset, case_y - screw_inset])
                translate([x, y, -0.1])
                    cylinder(d = m2_5_clearance_dia, h = floor_thickness + 0.2);
    }

    // Relay board standoffs from inside the lid
    rb_cx = case_x/2;
    rb_cy = case_y/2 - 5;
    standoff_h = 4;  // raise board off the lid floor so leads don't short
    for (x = [-relay_hole_spacing_x/2, relay_hole_spacing_x/2])
        for (y = [-relay_hole_spacing_y/2, relay_hole_spacing_y/2])
            translate([rb_cx + x, rb_cy + y, floor_thickness])
                standoff(standoff_h, m3_standoff_outer_dia, m3_clearance_dia);
}

// ──────────────────────────────────────────────────────────
// RENDER
// ──────────────────────────────────────────────────────────

if (top_only) {
    rotate([180, 0, 0])
        translate([0, 0, -top_height])
            top();
} else if (bottom_only) {
    bottom();
} else {
    // Preview both stacked (for visualization only, not for slicing)
    bottom();
    translate([0, 0, bottom_height + 1])
        top();
}

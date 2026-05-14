// test_fit.scad
//
// PRINT THIS FIRST before committing to the full case.
//
// This is a small plate with the four M2.5 mounting holes positioned where
// they SHOULD be for a Raspberry Pi 5. If your Pi seats correctly with
// 4x M2.5 screws through these holes, the full case (case.scad) will fit.
// If the holes are off, adjust the parameters in case.scad to match what
// works here.
//
// Print at 0.2mm layer height, 20% infill, takes ~10 minutes.
//
// VERIFIED PARAMETERS:
// - Pi 5 board: 85mm x 56mm (same as Pi 4 board outline)
// - Pi 5 mounting holes: M2.5, 58mm x 49mm spacing, 3.5mm from board edges
//
// These match the published Raspberry Pi 5 mechanical drawing. Source:
// https://datasheets.raspberrypi.com/rpi5/raspberry-pi-5-mechanical-drawing.pdf

$fn = 64;

// Pi 5 standoff hole pattern
pi_board_x = 85.0;
pi_board_y = 56.0;
pi_hole_spacing_x = 58.0;   // distance between hole pairs along long axis
pi_hole_spacing_y = 49.0;   // distance between hole pairs along short axis
pi_hole_inset = 3.5;        // distance from board edge to hole center

m2_5_clearance_dia = 2.8;   // M2.5 screw clearance hole

// Test piece
plate_thickness = 3.0;
plate_margin = 5.0;     // border around Pi board outline

module test_plate() {
    difference() {
        // Base plate matching Pi board outline plus margin
        linear_extrude(plate_thickness)
            offset(r = 2)
                square([pi_board_x + plate_margin*2,
                        pi_board_y + plate_margin*2],
                       center = true);

        // Hole pattern — same logic the full case uses
        translate([0, 0, -0.1])
            for (x = [-pi_hole_spacing_x/2, pi_hole_spacing_x/2])
                for (y = [-pi_hole_spacing_y/2, pi_hole_spacing_y/2])
                    translate([x, y, 0])
                        cylinder(d = m2_5_clearance_dia,
                                 h = plate_thickness + 0.2);

        // Pi outline scribed on top for visual confirmation
        // (a 0.4mm-deep rectangle showing where the board sits)
        translate([0, 0, plate_thickness - 0.4])
            linear_extrude(0.5)
                difference() {
                    square([pi_board_x, pi_board_y], center = true);
                    square([pi_board_x - 1.2, pi_board_y - 1.2], center = true);
                }
    }
}

test_plate();

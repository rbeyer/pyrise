#!/usr/bin/env python
"""Prepares an observation for color co-registration and processing."""

# Copyright 2007-2020, Arizona Board of Regents on behalf of the Lunar and
# Planetary Laboratory at the University of Arizona.
#   - Orignal Perl program.
#
# Copyright 2020, Ross A. Beyer (rbeyer@seti.org)
#   - Elements of this Python program are are based on the original Perl
#     but the logic here is rewritten from scratch to emulate functionality.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# This program is based on HiColor version 5.4.3 (2020/04/28)
# and on the Perl HiColorInit program ($Revision: 1.39 $
#                                      $Date: 2020/04/28 16:56:16 $)
# by Guy McArthur as an employee of the University of Arizona.

import argparse
import logging
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import kalasiris as isis
import hiproc.hirise as hirise
import hiproc.util as util


CCD_Corresponence = {
    "IR10": "RED4",
    "IR11": "RED5",
    "BG12": "RED4",
    "BG13": "RED5",
}


class HiColorCube(hirise.CCDID):
    """A class for HiRISE CCD IDs with additional capabilities for HiColor."""

    def __init__(self, pathlike):

        self.path = Path(pathlike)
        super().__init__(hirise.get_CCDID_fromfile(self.path))
        self.bin = int(isis.getkey_k(self.path, "Instrument", "Summing"))
        self.tdi = int(isis.getkey_k(self.path, "Instrument", "TDI"))
        self.lines = int(isis.getkey_k(self.path, "Dimensions", "Lines"))
        self.samps = int(isis.getkey_k(self.path, "Dimensions", "Samples"))

    def __repr__(self):
        return f"{self.__class__.__name__}('{self.path}')"


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, parents=[util.parent_parser()]
    )
    parser.add_argument(
        "-o", "--output_suffix", required=False, default=".precolor.cub"
    )
    parser.add_argument("cubes", metavar="balance.cub-files", nargs="+")

    args = parser.parse_args()

    util.set_logging(args.log)

    if not args.output_suffix.startswith("."):
        logging.critical(
            "--output_suffix must start with a period, and it "
            f"does not: {args.output_suffix}"
        )
        sys.exit()

    try:
        start(args.cubes, args.output_suffix, keep=args.keep)
    except ValueError as err:
        logging.critical(err)
        sys.exit()
    except subprocess.CalledProcessError as err:
        print("Had an ISIS error:")
        print(" ".join(err.cmd))
        print(err.stdout)
        print(err.stderr)
        raise err
    return


def start(cube_paths: list, output_suffix: str, keep=False):

    cubes = list(map(HiColorCube, cube_paths))
    (red4, red5, ir10, ir11, bg12, bg13) = separate_ccds(cubes)

    if red4 is None and red5 is None:
        raise ValueError("Neither RED4 nor RED5 were provided.")

    if red4 is not None and red5 is not None:
        if red4.lines != red5.lines or red4.samps != red5.samps:
            raise ValueError("RED4 dimensions not equal to RED5 dimensions!")

    if red4 is not None:
        HiColorInit(red4, ir10, bg12, output_suffix, keep=keep)
    if red5 is not None:
        HiColorInit(red5, ir11, bg13, output_suffix, keep=keep)
    return


def separate_ccds(cubes: list) -> tuple:
    """Return a tuple of six values, either HiColorCubes, or None."""
    red4 = red5 = ir10 = ir11 = bg12 = bg13 = None
    for c in cubes:
        if c.ccdnumber == "4":
            red4 = c
        elif c.ccdnumber == "5":
            red5 = c
        elif c.ccdnumber == "10":
            ir10 = c
        elif c.ccdnumber == "11":
            ir11 = c
        elif c.ccdnumber == "12":
            bg12 = c
        elif c.ccdnumber == "13":
            bg13 = c
        else:
            # Not one of the six that we care about,
            # so we'll silently ignore it.
            pass
    return (red4, red5, ir10, ir11, bg12, bg13)


def HiColorInit(
    red: HiColorCube,
    ir: HiColorCube,
    bg: HiColorCube,
    outsuffix: str,
    keep=False,
):
    """Do all of the scaling and cropping for a RED/IR/BG set."""

    if (
        len(
            set(
                map(
                    lambda c: c.get_obsid(),
                    filter(lambda x: x is not None, [red, ir, bg]),
                )
            )
        )
        != 1
    ):
        raise ValueError(
            "These cube files don't all have the same "
            f"Observation ID: {red}, {ir}, {bg}"
        )

    temp_token = datetime.now().strftime("HiColorInit-%y%m%d%H%M%S")

    # These two values are calculated but only written to a PVL file,
    # which I think we can skip.
    # # in bin1 there are 48 pixels of overlap
    # total_width = 2 * red.samps - (48 / red.bin)
    #
    # # in bin1, the right half starts at pixel 2001
    # image_midpoint = 2000 / red.bin + 1

    for c in filter(lambda x: x is not None, [ir, bg]):
        # Calculate delta offset in lines between red and color ccd
        offset = int((200 * (c.bin - red.bin) + c.tdi - red.tdi) / red.bin)

        bin_ratio = c.bin / red.bin
        # tdi_ratio = c.tdi / red.tdi
        mag_ratio = bin_ratio / 1.0006
        # ratio of color to red for enlargement, correction of optical
        # distortion from OPTICAL_ENLARGEMENT_RATIO constant in original
        # HiColor.pm

        # Rescale the color by the bin ratio and mag ratio, to match the red.
        # These will be the BG and IR "pre-color" cubes.
        rescaled = c.path.with_suffix(f".{temp_token}.rescaled" + outsuffix)

        if mag_ratio < 1:
            s = 1 / mag_ratio
            isis.reduce(
                c.path,
                to=rescaled,
                sscale=s,
                lscale=bin_ratio,
                validper=1,
                algorithm="nearest",
                vper_replace="nearest",
            )
        else:
            isis.enlarge(
                c.path,
                to=rescaled,
                sscale=mag_ratio,
                lscale=bin_ratio,
                interp="bilinear",
            )

        # The original Perl had an additional step to divide c.bin by the
        # bin_ratio, and provide that to value= below, but that's
        # mathematically just red.bin, so we'll skip a calculation:
        isis.editlab(
            rescaled,
            options="modkey",
            grpname="Instrument",
            keyword="Summing",
            value=red.bin,
        )

        # trim by placing in a new image
        isis.handmos(
            rescaled,
            mosaic=c.path.with_suffix(outsuffix),
            create="Y",
            nlines=red.lines,
            nsamp=red.samps,
            nband=1,
            outline=offset,
            outsamp=1,
            outband=1,
        )

        if not keep:
            rescaled.unlink()
    return

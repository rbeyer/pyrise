#!/usr/bin/env python
"""Stitch together two HiRISE channel files to create a single CCD image
file."""

# Copyright 2004-2020, Arizona Board of Regents on behalf of the Lunar and
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
# This program is based on HiStitch version 2.20.3 (2020/04/28),
# and on the Perl HiStitch program ($Revision: 1.26 $
#                                   $Date: 2020/04/28 17:52:53 $)
# and on the Perl HiFurrow_Fix program ($Revision: 1.11 $
#                                       $Date: 2020/04/28 17:52:53 $)
# by Eric Eliason as an employee of the University of Arizona.

import argparse
import collections
import json
import logging
import os
import shutil
from datetime import datetime
from pathlib import Path

import pvl

import kalasiris as isis
import hiproc.hirise as hirise
import hiproc.util as util


def main():
    parser = argparse.ArgumentParser(
        description=__doc__,
        parents=[util.parent_parser()],
        conflict_handler="resolve",
    )
    parser.add_argument(
        "--db",
        required=False,
        default=".HiCat.json",
        help="The .json file to use.  Optionally, if it "
        "starts with a '.' it is considered an extension and "
        "will be swapped with the input file's extension to "
        "find the .json file to use.",
    )
    parser.add_argument(
        "--db2",
        required=False,
        default=".HiCat.json",
        help="The second .json file to use.  Optionally, if "
        "it starts with a '.' it is considered an extension "
        "and will be swapped with the second input file's "
        "extension to find the .json file to use.",
    )
    parser.add_argument("--dbout", required=False, default=".HiCat.json")
    parser.add_argument(
        "-o", "--output", required=False, default=".HiStitch.cub"
    )
    parser.add_argument(
        "-c",
        "--conf",
        required=False,
        default=Path(__file__).resolve().parent.parent
        / "data"
        / "HiStitch.conf",
    )
    parser.add_argument("cube0", metavar="cube0.cub-file")
    parser.add_argument("cube1", metavar="cube1.cub-file", nargs="?")

    # The original Perl needed the specific output of cubenorm from a
    # particular step in the HiCal pipeline before here.  However, rather
    # than keep track of those files, open them, and read them again, we
    # can (and did) perform the relevant check in HiCal.py, and then save
    # that in the db that we can check now.

    args = parser.parse_args()

    util.set_logging(args.log)

    (db, outcub_path) = start(
        args.cube0,
        args.cube1,
        get_db(util.pid_path_w_suffix(args.db, args.cube0)),
        get_db(util.pid_path_w_suffix(args.db2, args.cube1)),
        args.output,
        args.conf,
        keep=args.keep,
    )

    db_path = set_outpath(
        args.dbout, hirise.get_CCDID_fromfile(outcub_path), outcub_path.parent
    )

    with open(db_path, "w") as f:
        json.dump(db, f, indent=0, sort_keys=True)


def start(
    cube0: os.PathLike,
    cube1: os.PathLike,
    db0: dict,
    db1: dict,
    output: os.PathLike,
    conf_path: os.PathLike,
    keep=False,
) -> dict:

    # GetConfigurationParameters()
    conf = pvl.load(str(conf_path))
    conf_check(conf)

    # GetProductFiles()
    if not cube1:
        cubes = (Path(cube0),)
    else:
        cubes = sort_input_cubes(Path(cube0), Path(cube1))

    chids = get_chids(cubes)

    outcub_path = set_outpath(output, hirise.CCDID(chids[0]), cubes[0].parent)

    # PrepareDBStatements()
    #   select from HiCat.EDR_Products
    db_paths = list()
    db_paths.append(db0)
    if len(cubes) == 2:
        db_paths.append(db1)

    dbs = sort_databases([db0, db1], chids)

    (truthchannel, balanceratio) = HiStitch(
        cubes,
        outcub_path,
        conf["HiStitch"],
        dbs,
        int(chids[0].ccdnumber),
        keep=keep,
    )

    # insert ObsID, pid0.ccdname+pid0.ccdnumber, truthchannel, balanceratio
    # into HiCat.CCD_Processing_Statistics
    db = dict()
    db["OBSERVATION_ID"] = str(chids[0].get_obsid())
    db["CCD"] = chids[0].get_ccd()
    db["CONTROL_CHANNEL"] = truthchannel
    db["CHANNEL_MATCHING_CORRECTION"] = balanceratio

    return (db, outcub_path)


def get_db(db_path: os.PathLike) -> dict:
    if db_path is None:
        return None

    with open(db_path) as f:
        db = json.load(f)

    return db


def get_chids(cubes: list) -> tuple:

    cid_a = hirise.get_ChannelID_fromfile(cubes[0])

    if len(cubes) == 2:
        cid_b = hirise.get_ChannelID_fromfile(cubes[1])
        if hirise.CCDID(cid_a) == hirise.CCDID(cid_b):
            return (cid_a, cid_b)
        else:
            raise ValueError(
                "These do not appear to be channels "
                f"from the same CCD:\n{cubes[0]}: "
                f"{cid_a} and\n{cubes[1]}: {cid_b}"
            )
    else:
        return (cid_a,)


def set_outpath(
    output: os.PathLike, cid: hirise.CCDID, directory: Path
) -> Path:
    if str(output).startswith("."):
        return directory / Path(str(cid) + output)
    else:
        return Path(output)


def sort_input_cubes(a_cub: os.PathLike, b_cub: os.PathLike) -> tuple:
    """Figures out which one is Channel 0 and which is Channel 1."""

    channel_of = dict()
    channel_of[a_cub] = isis.getkey_k(a_cub, "Instrument", "ChannelNumber")
    channel_of[b_cub] = isis.getkey_k(b_cub, "Instrument", "ChannelNumber")

    if channel_of[a_cub] == channel_of[b_cub]:
        raise RuntimeError(
            f"{a_cub} and {b_cub} have the same channel: "
            "{channel_of[a_cub]}."
        )

    for (cub, chan) in channel_of.items():
        if int(chan) != 0 and int(chan) != 1:
            raise RuntimeError(
                f"{cub} has a channel other than 0 or 1: {chan}"
            )

    sorted_cubchan = sorted(channel_of.items(), key=lambda x: x[1])
    return (sorted_cubchan[0][0], sorted_cubchan[1][0])


def sort_databases(dbs: list, chids: tuple) -> tuple:
    """Ensures that the databases match the cubes."""

    if len(dbs) != len(chids):
        raise IndexError(
            f"The number of databases {len(dbs)}  does not "
            "match the number of Product IDs {chids}"
        )

    chid_db_map = dict()
    for p in chids:
        for d in dbs:
            if str(p) == d["PRODUCT_ID"]:
                chid_db_map[p] = d
                break
    if len(chid_db_map) != len(chids):
        raise LookupError(
            "The Product IDs in the databases " f"do not match {chids}"
        )
    return tuple(map(chid_db_map.get, chids))


def HiStitch(
    cubes: list,
    out_cube: os.PathLike,
    conf: dict,
    dbs: list,
    ccd_number: int,
    keep=False,
) -> tuple:
    logging.info("HiStitch start.")

    out_path = Path(out_cube)
    # Allows for indexing in lists ordered by bin value.
    b = 1, 2, 4, 8, 16

    # This string will get placed in the filename for all of our
    # temporary files. It will (hopefully) prevent collisions with
    # existing files and also allow for easy clean-up if keep=True
    temp_token = datetime.now().strftime("HiStitch-%y%m%d%H%M%S")

    # ProcessingStep() - mostly sets up stuff
    max_mean = max(map(lambda x: float(x["IMAGE_MEAN"]), dbs))
    flags = set_flags(
        conf, dbs, ccd_number, b.index(int(dbs[0]["BINNING"])), max_mean
    )

    # HiStitchStep() - runs HiStitch, and originally inserted to db, now we
    # just return at the bottom of this function.
    logging.info(
        HiStitchStep(
            cubes,
            out_path,
            dbs[0]["BINNING"],
            ccd_number,
            conf,
            flags.balance,
            flags.equalize,
        ).args
    )

    if len(cubes) == 2 and flags.balance:
        # run getkey from ?HiStitch_output?
        truthchannel = isis.getkey_k(out_path, "HiStitch", "TruthChannel")
        balanceratio = isis.getkey_k(out_path, "HiStitch", "BalanceRatio")
    else:
        truthchannel = None
        balanceratio = None

    if flags.furrow:
        furrow_file = out_path.with_suffix(f".{temp_token}.temp.cub")
        HiFurrow_Fix(out_path, furrow_file, max_mean, keep=keep)
        furrow_file.rename(out_path)

    logging.info("HiStitch done.")
    return (truthchannel, balanceratio)


def set_flags(
    conf, dbs, ccdnum: int, bindex: int, max_mean: float
) -> collections.namedtuple:
    """Set various processing flags based on various configuration
    parameters."""
    HiStitchFlags = collections.namedtuple(
        "HiStitchFlags", ["furrow", "balance", "equalize"]
    )

    max_lis = max(map(lambda x: int(x["LOW_SATURATED_PIXELS"]), dbs))
    max_std = max(map(lambda x: float(x["CAL_MASK_STANDARD_DEVIATION"]), dbs))
    max_dstd = max(
        map(lambda x: float(x["IMAGE_DARK_STANDARD_DEVIATION"]), dbs)
    )
    max_gper = max(map(lambda x: float(x["GAP_PIXELS_PERCENT"]), dbs))

    binning = int(dbs[0]["BINNING"])

    # We don't run FurrowCheck() here like the original Perl does, but do it
    # earlier in HiCal.py and store the result to get now.

    # Determine if the Furrow Normalization is to occur
    furrow = False
    if (
        not any(map(lambda x: bool(x["zapped"]), dbs))
        and max_mean >= float(conf["HiStitch_Furrow_Normalization"])
        and (binning == 2 or binning == 4)
    ):
        furrow = True

    # Determine if the balance or equalize options are to occur
    balance = False
    equalize = False

    if conf["HiStitch_Balance"] or conf["HiStitch_Equalize"]:
        if int(conf["HiStitch_Balance_Processing"][ccdnum]) == 0 or (
            max_lis < int(conf["HiStitch_LIS_Pixels"])
            # The original Perl had these two following conditions written
            # this way, but I think the lists need to be indexed by bindex:
            and max_std < float(conf["HiStitch_Balance_Bin_Mask_STD"][binning])
            and max_dstd
            < float(conf["HiStitch_Balance_Bin_DarkPixel_STD"][binning])
        ):
            logging.warning(
                "Original Perl issue: conf file arrays are being "
                "indexed incorrectly, may affect setting of "
                "equalize and balance flags."
            )
            # To correct, change the indexing of the conf lists above with
            # 'bindex' instead of 'binning'.

            # equalize and balance can't both be true
            if conf["HiStitch_Equalize"] and max_gper < float(
                conf["HiStitch_Gap_Percent"]
            ):
                equalize = True
            elif conf["HiStitch_Balance"]:
                balance = conf["HiStitch_Balance"]

    return HiStitchFlags(furrow, balance, equalize)


def HiStitchStep(in_cubes, out_cub, binning, ccdnum, conf, balance, equalize):
    zpad_bin = "{:0>2}".format(binning)
    histitch_args = {"from1": in_cubes[0], "to": out_cub}
    if len(in_cubes) == 2:
        histitch_args["from2"] = in_cubes[1]
    if not balance and not equalize:
        histitch_args["balance"] = False
    if equalize or balance:
        histitch_args["skip"] = conf[f"HiStitch_Bin{zpad_bin}_Area"][0]
        histitch_args["seamsize"] = conf[f"HiStitch_Bin{zpad_bin}_Area"][1]
        histitch_args["channel"] = conf["HiStitch_Control_Channel"][ccdnum]
        if balance:
            histitch_args["balance"] = "TRUE"
        if equalize:
            histitch_args["balance"] = "EQUALIZE"
            histitch_args["width"] = conf["HiStitch_Equalize_Width"]
            histitch_args["operator"] = conf["HiStitch_Equalize_Correction"]

    return isis.histitch(**histitch_args)


def HiFurrow_Fix(
    in_cube: os.PathLike, out_cube: os.PathLike, max_mean: float, keep=False
):
    """Perform a normalization of the furrow region of bin 2 or 4
    HiRISE images. The input to this script is a HiRISE stitch
    product containing both channels of a CCD.
    """
    in_cub = Path(in_cube)

    binning = int(isis.getkey_k(in_cub, "Instrument", "Summing"))
    lines = int(isis.getkey_k(in_cub, "Dimensions", "Lines"))
    samps = int(isis.getkey_k(in_cub, "Dimensions", "Samples"))

    if binning != 2 and binning != 4:
        raise ValueError(
            "HiFurrow_Fix only supports correction for " "bin 2 or 4 data."
        )
    if binning == 2 and samps != 1024:
        raise ValueError(
            f"HiFurrowFix: improper number of samples: {samps}, "
            "for a stitch product with bin 2 (should be 1024)."
        )

    # This string will get placed in the filename for all of our
    # temporary files. It will (hopefully) prevent collisions with
    # existing files and also allow for easy clean-up if keep=True
    temp_token = datetime.now().strftime("HFF-%y%m%d%H%M%S")
    to_del = isis.PathSet()

    # For bin2 and bin4 imaging, specify width of furrow based on
    # image average DN range
    range_low = {2: (512, 513), 4: (256, 257)}  # 2 pixel furrow width
    range_mid = {2: (511, 514), 4: (255, 258)}  # 4 pixel furrow width
    range_hgh = {2: (511, 514), 4: (255, 258)}  # 4 pixel furrow width
    range_max = {2: (510, 515), 4: (254, 259)}  # 6 pixel furrow width

    # Original code had low/mid/hgh for bin2 and bin4, but they
    # were hard-coded to be identical.
    dn_range_low = 9000
    dn_range_mid = 10000
    dn_range_hgh = 12000

    if max_mean > dn_range_hgh:
        dn_range = range_max[binning]
    elif max_mean > dn_range_mid:
        dn_range = range_hgh[binning]
    elif max_mean > dn_range_low:
        dn_range = range_mid[binning]
    else:
        dn_range = range_low[binning]

    lpf_samp = int((dn_range[1] - dn_range[0] + 1) / 2) * 4 + 1
    lpf_line = int(lpf_samp / 2) * 20 + 1

    # Create a mask file
    # DN=1 for non-furrow area
    # DN=0 for furrow area
    eqn = rf"\(1*(sample<{dn_range[0]})+ 1*(sample>{dn_range[1]}) + 0)"
    fx_cub = to_del.add(in_cub.with_suffix(f".{temp_token}.fx.cub"))
    isis.fx(
        to=fx_cub, mode="OUTPUTONLY", lines=lines, samples=samps, equation=eqn
    )

    # Create a file where the furrow area is set to null
    mask1_cub = to_del.add(in_cub.with_suffix(f".{temp_token}.mask1.cub"))
    isis.mask(
        in_cub,
        mask=fx_cub,
        to=mask1_cub,
        min_=1,
        max_=1,
        preserve="INSIDE",
        spixels="NULL",
    )

    # Lowpass filter to fill in the null pixel area
    lpf_cub = to_del.add(in_cub.with_suffix(f".{temp_token}.lpf.cub"))
    isis.lowpass(
        mask1_cub,
        to=lpf_cub,
        sample=lpf_samp,
        line=lpf_line,
        null=True,
        hrs=False,
        his=False,
        lis=False,
    )

    # Create a file where non-furrow columns are set to null
    mask2_cub = to_del.add(in_cub.with_suffix(f".{temp_token}.mask2.cub"))
    isis.mask(
        in_cub,
        mask=fx_cub,
        to=mask2_cub,
        min_=0,
        max_=0,
        preserve="INSIDE",
        spixels="NULL",
    )

    # Highpass filter the furrow region
    hpf_cub = to_del.add(in_cub.with_suffix(f".{temp_token}.hpf.cub"))
    isis.highpass(mask2_cub, to=hpf_cub, sample=1, line=lpf_line)

    # Add lowpass and highpass together to achieve desired result
    alg_cub = to_del.add(in_cub.with_suffix(f".{temp_token}.alg.cub"))
    isis.algebra(
        from_=lpf_cub, from2=hpf_cub, to=alg_cub, operator="ADD", A=1.0, B=1.0
    )

    # copy the input file to the output file then mosaic the
    # furrow area as needed.
    logging.info(f"Copy {in_cub} to {out_cube}.")
    shutil.copyfile(in_cub, out_cube)
    isis.handmos(
        alg_cub,
        mosaic=out_cube,
        outsample=1,
        outline=1,
        outband=1,
        insample=1,
        inline=1,
        inband=1,
        create="NO",
    )

    if not keep:
        to_del.unlink()

    return


def conf_check(conf: dict) -> None:
    """Various checks on parameters in the configuration."""

    util.conf_check_strings(
        "HiStitch_Clean_Files",
        ("DELETE", "KEEP"),
        conf["HiStitch"]["HiStitch_Clean_Files"],
    )

    util.conf_check_bool(
        "HiStitch_Balance", conf["HiStitch"]["HiStitch_Balance"]
    )

    util.conf_check_bool(
        "HiStitch_Equalize", conf["HiStitch"]["HiStitch_Equalize"]
    )

    util.conf_check_strings(
        "HiStitch_Equalize_Correction",
        ("MULTIPLY", "ADD"),
        conf["HiStitch"]["HiStitch_Equalize_Correction"],
    )

    util.conf_check_count(
        "HiStitch_Balance_Processing",
        14,
        "CCD",
        conf["HiStitch"]["HiStitch_Balance_Processing"],
    )

    util.conf_check_count(
        "HiStitch_Control_Channel",
        14,
        "CCD",
        conf["HiStitch"]["HiStitch_Control_Channel"],
    )

    util.conf_check_count(
        "HiStitch_Balance_Bin_DarkPixel_STD",
        5,
        "bin mode",
        conf["HiStitch"]["HiStitch_Balance_Bin_DarkPixel_STD"],
    )

    util.conf_check_count(
        "HiStitch_Balance_Bin_Mask_STD",
        5,
        "bin mode",
        conf["HiStitch"]["HiStitch_Balance_Bin_Mask_STD"],
    )

    util.conf_check_bounds(
        "HiStitch_Normalization_Minimum",
        (-16384.0, 16364.0),
        conf["HiStitch"]["HiStitch_Normalization_Minimum"],
    )

    util.conf_check_bounds(
        "HiStitch_Normalization_Maximum",
        (-16384.0, 16364.0),
        conf["HiStitch"]["HiStitch_Normalization_Maximum"],
    )

    util.conf_check_bounds(
        "HiStitch_Minimum_Percent",
        (0, 2),
        conf["HiStitch"]["HiStitch_Minimum_Percent"],
    )
    return


if __name__ == "__main__":
    main()

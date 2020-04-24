#!/usr/bin/env python
"""Deal with stuck bits in HiRISE pixels.

If there is some process which is flipping one (or more) of the
HiRISE bits in a 14-bit pixel the wrong way, it will inadvertently
add or subtract a certain DN value to a pixel.  Naturally, it goes
from 2^13 (8192) all the way down to 2.

In practice, these 'bit-flipped' pixels show up as spikes in the
image histogram at DN values approximately centered on the image
Median plus or minus the bit-flip value (8192, 4096, etc.).  These
are not narrow spikes and have variable width, so care must be taken
when dealing with them.  The large bit-flip values often show up
as easily-identifiable islands in the DN histogram, but small
bit-flip values (2, 4, 8, etc.) are often within the variability
of the data and it is more difficult to cleanly isolate them.

This library and program deals with two aspects of the bit-flip
problem: identification and mitigation.


Identification
--------------

This can be as simple as deciding that some bit-flip position (maybe
16 or 32) is the cut-off, and simply saying that the DN window of
'good' pixles is Median plus/minus the cut-off.  However, this is
often a blunt instrument, and can still leave problematic outliers.

An improved strategy is to attempt to identify the 'islands' in the
DN histogram that are centered on the bit-flip positions, and attempt
to find the gaps between those islands.  Again, picking a bit-flip
value cut-off, but this time selecting the low-point between two
bit-flip-value-centered islands as the boundaries of the good-DN-window.
This strategy works okay for test data, but real data of the martian
surface often has complicated histograms, so something a little more
robust is needed.

The best strategy we have developed so far is to take the above one
step further and treat the histogram curve as its own signal,
identify all of the minima in the histogram, along with a sense of
the standard deviation of the 'good' data in the image, and attempt
to smartly pick the bounds of the good-DN-window.


Mitigation
----------
This library and the program can perform one of two general activities
to mitigate the bit-flip pixels once they have been identified.

1. Unflipping: It can attempt to un-flip the bits in the pixel to
   attempt to return it to what its 'original' DN value might have
   been.  This is limited by however you select the bit-flip cut-off.
   Since you can't identify below that cut-off, you also really can't
   unflip below that cut-off.

2. Masking: This mitigation strategy simply sets these pixels to
   an arbitrary DN value or the ISIS NULL value.  This basically just
   exercises the 'identification' part of this library and leaves
   it up to other image processing activities to decide how to deal
   with this data.
"""

# Copyright 2020, Ross A. Beyer (rbeyer@seti.org)
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
# Parts of this were inspired by clean_bit_flips.pro by Alan Delamere,
# Oct 2019, but the implementation here was written from scratch.

import argparse
import itertools
import logging
import math
import os
import shutil
import subprocess
from pathlib import Path

import numpy as np
from scipy.signal import find_peaks

import pvl

import pyrise.hirise as hirise
import pyrise.util as util
import kalasiris as isis


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     parents=[util.parent_parser()])
    parser.add_argument('-o', '--output',
                        required=False, default='.bitflip.cub')
    parser.add_argument('-m', '--mask', required=False, action='store_true',
                        help=('If set, the program will mask the '
                              'bit-flipped pixels, otherwise will '
                              'try to collapse them.'))
    parser.add_argument('--line', required=False, action='store_true',
                        help='In conjunction with -m applies cubenorm in '
                        'the line direction instead of column.')
    parser.add_argument('cube', metavar="some.cub-file", nargs='+',
                        help='More than one can be listed here.')

    args = parser.parse_args()

    util.set_logging(args.log, args.logfile)

    if(len(args.cube) > 1 and
       not args.output.startswith('.')):
        logging.critical('With more than one input cube file, the --output '
                         'must start with a period, and it '
                         f'does not: {args.output}')
        sys.exit()

    for i in args.cube:
        out_p = util.path_w_suffix(args.output, i)

        try:
            if args.mask:
                mask(Path(i), out_p, line=args.line, keep=args.keep)
            else:
                unflip(Path(i), out_p, keep=args.keep)
        except subprocess.CalledProcessError as err:
            print('Had an ISIS error:')
            print(' '.join(err.cmd))
            print(err.stdout)
            print(err.stderr)
            raise err
    return


def get_range(start, stop, step=None) -> range:
    """Returns a range object given floating point start and
    stop values (the optional step must be an int).  If
    step is none, it will be set to 1 or -1 depending on the
    relative values of start and stop.
    """
    if start < stop:
        start = math.floor(start)
        stop = math.ceil(stop)
        if step is None:
            step = 1
    else:
        start = math.ceil(start)
        stop = math.floor(stop)
        if step is None:
            step = -1

    return range(start, stop, step)


def find_gap(hist: list, start, stop, step=None, findfirst=False) -> int:
    """Returns a DN between *start* and *stop* which is determined to be a gap.

    Given the range specified by *start*, *stop* and *step*, the
    DNs are extracted from the list of namedtuples (which must
    conform to the HistRow namedtuple from kalasiris.Histogram).
    The 'gaps' between continuous ranges of DN are found (DNs where
    the histogram has no pixels).  The largest gap is found, and
    then the DN value closest to start from that largest gap is
    returned.

    If step is not specified or None, it will be set to 1 or -1
    depending on the relative values of start and stop.

    If *findfirst* is `True`, then rather than finding the 'biggest'
    gap, it will return the DN from the gap that is closest to
    *start*.
    """
    dn_window = get_range(start, stop, step)

    hist_set = set(filter(lambda d: d in dn_window,
                          (int(x.DN) for x in hist)))

    missing = sorted(set(dn_window).difference(hist_set),
                     reverse=(dn_window.step < 0))
    # logging.info(f'{bs} missing: ' + str(missing))

    if len(missing) > 0:
        sequences = list()
        for k, g in itertools.groupby(missing,
                                      (lambda x,
                                       c=itertools.count(): next(c) -
                                       (dn_window.step * x))):
            sequences.append(list(g))

        if findfirst:
            return sequences[0][0]
        else:
            # find biggest gap
            return max(sequences, key=len)[0]
    else:
        # There was no gap in DN.
        raise ValueError('There was no gap in the DN window from '
                         f'{start} to {stop}.')


def find_min_dn(hist: list, start, stop, step=None) -> int:
    """Returns the DN from *hist* with the lowest pixel count.

    Where *hist* is a list of namedtuples (which must conform to
    the HistRow namedtuple from kalasiris.Histogram).  The *start*,
    *stop*, and *step* parameters are DN values in that *hist* list.

    Given how the min() mechanics work, this should return the first
    DN value of the entry with the lowest pixel count, in range order
    (if there is more than one).
    """
    r = get_range(start, stop, step)
    hist_list = sorted(filter(lambda x: int(x.DN) in r, hist),
                       key=lambda x: int(x.DN),
                       reverse=(r.step < 0))

    h = min(hist_list, key=lambda x: int(x.Pixels))
    return int(h.DN)


def subtract_over_thresh(in_path: Path, out_path: Path,
                         thresh: int, delta: int, keep=False):
    """This is a convenience function that runs ISIS programs to add or
    subtract a value to DN values for pixels that are above or below
    a threshold.

    For all pixels in the *in_path* ISIS cube, if *delta* is positive,
    then pixels with a value greater than *thresh* will have *delta*
    subtracted from them.  If *delta* is negative, then all pixels
    less than *thresh* will have *delta* added to them.
    """

    # Originally, I wanted to just do this simply with fx:
    # eqn = "\(f1 + ((f1{glt}={thresh}) * {(-1 * delta)}))"
    # However, fx writes out floating point pixel values, and we really
    # need to keep DNs as ints as long as possible.  Sigh.

    shutil.copyfile(in_path, out_path)

    mask_p = in_path.with_suffix('.threshmask.cub')
    mask_args = {'from': in_path, 'to': mask_p}
    if delta > 0:
        mask_args['min'] = thresh
    else:
        mask_args['max'] = thresh
    util.log(isis.mask(**mask_args).args)

    delta_p = in_path.with_suffix('.delta.cub')
    util.log(isis.algebra(mask_p, from2=in_path, to=delta_p,
                          op='add', a=0, c=(-1 * delta)).args)

    util.log(isis.handmos(delta_p, mosaic=out_path).args)

    if not keep:
        mask_p.unlink()
        delta_p.unlink()

    return


def histogram(in_path: Path, hist_path: Path):
    """This is just a convenience function to facilitate logging."""
    util.log(isis.hist(in_path, to=hist_path).args)
    return isis.Histogram(hist_path)


def clean_cube(cube: os.PathLike, outcube: os.PathLike, width=5,
               cubenorm_path=None, plot=False, keep=False):
    """The file at *outcube* will be the result of running
    bit-flip cleaning of the file at *cube*.

    **WARNING**: at this time, only the image-area and the reverse-clock
    area are undergoing bit-flip cleaning.

    This means that pixels that are identified as being beyond the
    allowable DN window (which may be defined differently for each
    of the image areas), will be set to the ISIS NULL value.

    If ISIS cubenorm has already been run on *cube*, the path to
    the output can be specified with *cubenorm_path*.  If *cubenorm_path*
    is left unspecified, it will be created.

    If *keep* is True, then all intermediate files will be preserved,
    otherwise, this function will clean up any intermediary files
    it creates.
    """
    in_p = Path(cube)
    out_p = Path(cube)
    cn_p = None

    if cubenorm_path is None:
        cn_p = in_p.with_suffix('.cn.stats')
        util.log(isis.cubenorm(in_p, stats=cn_p).args)
    else:
        cn_p = Path(cubenorm_path)

    # Go bit-flip correct the non-image areas somehow.
    clean_tables(in_p, tableclean, width=width, keep=keep)

    results = pvl.loads(isis.stats(in_p).stdout)['Results']
    img_mean = float(results['Average'])
    img_mode = float(results['Mode'])
    img_median = float(results['Median'])

    hist_p = to_del.add(in_p.with_suffix('.hist'))
    util.log(isis.hist(in_p, to=hist_p).args)
    hist = isis.Histogram(hist_p)
    # median = math.trunc(float(hist['Median']))

    d = img_mean - img_mode
    logging.info(f'{temp_cube} Mean: {img_mean}, Mode: {img_mode}, '
                 f'diff: {d}, Median: {img_median}')

    medstd = median_std(cn_p)

    # Sometimes even the medstd can be too high because the 'good' lines
    # still had too many outliers in it.  What is 'too high' and how do
    # we define it rigorously?  I'm not entirely sure, and I wish there
    # was a better way to determine this, or, alternately an even more robust
    # way to find 'medstd' in the first place.
    # I am going to select an abitrary value based on my experience (300)
    # and then also pick a replacement of 64 which is a complete guess.
    # Finally, in this case, since the 'statistics' are completely broken,
    # I'm also going to set the exclusion to be arbitrary, and lower than
    # the enforced medstd.
    if medstd > 300:
        medstd = 64
        ex = 16
        logging.info('The derived medstd was too big, setting the medstd '
                     f'to {medstd} and the exclusion value to {ex}.')
    else:
        # We want to ignore minima that are too close to the central value.
        # Sometimes the medstd is a good choice, sometimes 16 DN (which is a
        # minimal bit flip level) is better, so use the greatest:
        ex = max(medstd, 16)

    mindn = img_median - (width * medstd)
    maxdn = img_median + (width * medstd)

    hist_list = sorted(hist, key=lambda x: int(x.DN))
    pixel_counts = np.fromiter((int(x.Pixels) for x in hist_list), int)
    dn = np.fromiter((int(x.DN) for x in hist_list), int)

    (sm_mindn, sm_maxdn) = find_smart_window(hist, mindn, maxdn, img_median,
                                             central_exclude_dn=ex, plot=plot)

    util.log(isis.mask(in_p, mask=in_p, to=out_p,
             minimum=sm_mindn, maximum=sm_maxdn,
             preserve='INSIDE', spixels='NONE').args)

    if not keep:
        to_del.unlink()

    return


def clean_tables(cube: Path, outcube: Path, width=5,
                 rev_area=True, mask_area=False, ramp_area=False,
                 buffer_area=False, dark_area=False,
                 keep=False):
    """The file at *outcube* will be the result of running bit-flip
    cleaning of the specified non-image areas in table objects within
    the ISIS cube file.

    The various *_area* booleans direct whether these areas will
    undergo bit-flip cleaning.

    **WARNING**: at this time, only the reverse-clock area is
    enabled for undergoing bit-flip cleaning.  Specifying True
    for any others will result in a NotImplementedError.

    This means that pixels that are identified as being beyond the
    allowable DN window (which may be defined differently for each
    of the image areas), will be set to the ISIS NULL value.

    If *keep* is True, then all intermediate files will be preserved,
    otherwise, this function will clean up any intermediary files
    it creates.

    This function anticipates a HiRISE cube that has the following
    table objects in its label: HiRISE Calibration Ancillary,
    HiRISE Calibration Image, HiRISE Ancillary.

    The HiRISE Calibration Ancillary table contains the BufferPixels
    and DarkPixels from either side of the HiRISE Calibration Image.
    The HiRISE Calibration Image table contains the Reverse-Clock,
    Mask, and Ramp Image areas.  The HiRISE Ancillary table contains
    the BufferPixels and DarkPixels from either side of the main
    Image Area.
    """
    for notimpl in (mask_area, ramp_area, buffer_area, dark_area):
        if notimpl is True:
            raise NotImplementedError(
                f"Bit-flip cleaning for {notimpl} is not yet implemented.")

    shutil.copy(cube, outcube)

    # for each area
    # read the table data
    # analyze the table data
    # filter the table data
    # write the table back out into outcube


def mask(in_path: Path, out_path: Path, line=False, plot=True, keep=False):
    """Attempt to mask out pixels beyond the central DNs of the median
    based on minima in the histogram."""
    from pyrise.HiCal import analyze_cubenorm_stats2

    to_del = isis.PathSet()

    hist_p = to_del.add(in_path.with_suffix('.hist'))
    hist = histogram(in_path, hist_p)

    median = math.trunc(float(hist['Median']))
    logging.info(f'Median: {median}')

    # With terrible noise, the computed median is the median of the
    # noise, not of the data, so if that happens, try and find a
    # DN peak that is more reasonable:
    median_limit = 4000
    if median > median_limit:
        trunc_hist = list()
        for row in hist:
            if int(row.DN) < median_limit:
                trunc_hist.append((int(row.DN), int(row.Pixels)))
        try:
            median = max(trunc_hist, key=lambda x: x[1])[0]
            logging.info(
                f'The median was too high, found a better one: {median}.')
        except ValueError:
            # No values in the trunc_hist, which means that there are no
            # DN less than maedian_limit, so we just leave things as they are.
            pass

    cubenorm_stats_file = to_del.add(in_path.with_suffix('.cn.stats'))
    if line:
        util.log(isis.cubenorm(in_path, stats=cubenorm_stats_file,
                               direction='line').args)
    else:
        util.log(isis.cubenorm(in_path, stats=cubenorm_stats_file).args)
    (mindn, maxdn) = analyze_cubenorm_stats2(cubenorm_stats_file, median,
                                             hist, width=5, plot=plot)

    # To bypass the 'medstd' calculations in analyze_cubenorm_stats2(),
    # this mechanism can be used to set the limits directly.
    # (mindn, maxdn) = find_smart_window(hist,
    #                                    math.trunc(float(hist['Minimum'])),
    #                                    math.trunc(float(hist['Maximum'])),
    #                                    median, plot=True)

    # util.log(isis.mask(in_path, to=out_path, minimum=maskmin,
    #                    maximum=maskmax).args)

    if not keep:
        to_del.unlink()
    return (mindn, maxdn)


def find_minima_index(central_idx: int, limit_idx: int,
                      minima_idxs: np.ndarray, pixel_counts: np.ndarray,
                      close_to_limit=True) -> int:
    """Searches the pixel_counts at the minima_idxs positions between
    central_idx and limit_idx to find the one with the lowest
    pixel_count value. If multiple lowest values, choose the closest
    to the limit_idx position (the default, if *close_to_limit* is true,
    otherwise picks the index closest to *central_idx* if *close_to_limit*
    is False).

    If no minima_idx values are found in the range, the next value
    in minima_idx past the limit_idx will be returned.
    """
    # print(f'central_idx: {central_idx}')
    # print(f'limit_idx: {limit_idx}')
    info_str = 'Looking for {} {} range ...'
    try:
        if limit_idx < central_idx:
            logging.info(info_str.format('min', 'inside'))
            inrange_iter = filter(lambda i: i < central_idx and i >= limit_idx,
                                  minima_idxs)
            if close_to_limit is False:
                select_idx = -1
            else:
                select_idx = 0
        elif limit_idx > central_idx:
            logging.info(info_str.format('max', 'inside'))
            inrange_iter = filter(lambda i: i > central_idx and i <= limit_idx,
                                  minima_idxs)
            if close_to_limit is False:
                select_idx = 0
            else:
                select_idx = -1
        else:
            raise ValueError

        inrange_i = np.fromiter(inrange_iter, int)
        logging.info(str(inrange_i) + ' are the indexes inside the range.')

        value = min(np.take(pixel_counts, inrange_i))
        logging.info(f'{value} is the minimum Pixel count amongst those '
                     'indexes.')

        idx = inrange_i[np.asarray(pixel_counts[inrange_i] ==
                                   value).nonzero()][select_idx]
    except ValueError:
        try:
            if limit_idx < central_idx:
                logging.info(info_str.format('min', 'outside'))
                idx = max(filter(lambda i: i < limit_idx, minima_idxs))
            elif limit_idx > central_idx:
                logging.info(info_str.format('max', 'outside'))
                idx = min(filter(lambda i: i > limit_idx, minima_idxs))
            else:
                raise ValueError

            logging.info(f'{idx} is the minimum index.')
        except ValueError:
            logging.info('Could not find a valid minima, returning '
                         f'the limit index: {limit_idx}.')
            idx = limit_idx

    return idx


def find_smart_window(dn: np.ndarray, counts: np.ndarray,
                      mindn: int, maxdn: int,
                      centraldn: int, central_exclude_dn=0,
                      plot=False, closest=True) -> tuple:
    '''Returns a minimum and maximum DN value from *dn* which are
       based on using the find_minima_index() function with the
       given *mindn*, *maxdn*, and *centraldn* values.  The *dn*
       array must be a sorted list of unique values, and *counts*
       is the number of times each of the values in *dn* occurs.

       If *central_exclude_dn* is given, the returned minimum and
       maximum DN are guaranteed to be at least *central_exclude_dn*
       away from *centraldn*.  This is useful if you don't want returned
       minimum and maximum DN to be too close to the *centraldn*.

       If plot is True, this function will display a plot describing
       its work.  The curve represents the hist values, the shaded
       area marks the window between the given mindn and maxdn.  The
       'x'es mark all the detected minima, and the red dots indicate
       the minimum and maximum DN values that this function will
       return.

       The value of *closest* is passed on to find_minima_index().
    '''
    # hist_list = sorted(hist, key=lambda x: int(x.DN))
    # pixel_counts = np.fromiter((int(x.Pixels) for x in hist_list), int)
    # dn = np.fromiter((int(x.DN) for x in hist_list), int)

    # print(dn)

    # print(f'mindn {mindn}')
    # print(f'maxdn {maxdn}')

    mindn_i = (np.abs(dn - mindn)).argmin()
    maxdn_i = (np.abs(dn - maxdn)).argmin()
    central_i = np.where(dn == centraldn)
    # print(f'centraldn: {centraldn}')
    # print(f'central_exclude_dn: {central_exclude_dn}')
    central_min_i = (np.abs(dn - (centraldn - central_exclude_dn))).argmin()
    # tobemin = np.abs(dn - (centraldn + central_exclude_dn))
    # print(tobemin)
    central_max_i = (np.abs(dn - (centraldn + central_exclude_dn))).argmin()

    minima_i, _ = find_peaks(np.negative(counts))

    # print(f'central_i {central_i}')
    # print(f'central_min_i {central_min_i}')
    # print(f'central_max_i {central_max_i}')
    # print(f'mindn_i {mindn_i}')
    # print(f'maxdn_i {maxdn_i}')

    min_i = find_minima_index(central_min_i, mindn_i, minima_i, counts,
                              close_to_limit=closest)
    max_i = find_minima_index(central_max_i, maxdn_i, minima_i, counts,
                              close_to_limit=closest)
    logging.info(f'indexes: {min_i}, {max_i}')
    logging.info(f'DN window: {dn[min_i]}, {dn[max_i]}')

    if plot:
        import matplotlib.pyplot as plt

        plt.ioff()
        fig, (ax0, ax1) = plt.subplots(2, 1)

        indices = np.arange(0, len(counts))
        dn_window = np.fromiter(map((lambda i: i >= mindn_i and i <= maxdn_i),
                                    (x for x in range(len(counts)))),
                                dtype=bool)
        ax0.set_ylabel('Pixel Count')
        ax0.set_xlabel('DN Index')
        ax0.set_yscale('log')
        ax0.fill_between(indices, counts, where=dn_window,
                         color='lightgray')
        ax0.axvline(x=central_i, c='gray')
        ax0.axvline(x=np.argmax(counts), c='lime', ls='--')
        ax0.plot(counts)
        ax0.plot(minima_i, counts[minima_i], "x")
        ax0.plot(min_i, counts[min_i], "o", c='red')
        ax0.plot(max_i, counts[max_i], "o", c='red')

        ax1.set_ylabel('Pixel Count')
        ax1.set_xlabel('DN')
        ax1.set_yscale('log')
        ax1.set_ybound(lower=0.5)
        ax1.scatter(dn, counts, marker='.', s=1)
        ax1.axvline(x=dn[min_i], c='red')
        ax1.axvline(x=dn[max_i], c='red')

        plt.show()

    return (dn[min_i], dn[max_i])


def mask_gap(in_path: Path, out_path: Path, keep=False):
    """Attempt to mask out pixels beyond the central DNs of the median
    based on gaps in the histogram.

    This approach worked well based on 'ideal' reverse-clocked data
    or 'dark' images, but in 'real' HiRISE images of Mars, the reality
    is that there are 'gaps' everywhere along the DN range, and this
    approach ends up being too 'dumb'.
    """

    to_del = isis.PathSet()

    hist_p = in_path.with_suffix('.hist')
    hist = histogram(in_path, hist_p)

    median = math.trunc(float(hist['Median']))
    # std = math.trunc(float(hist['Std Deviation']))

    high = find_gap(hist, median,
                    math.trunc(float(hist['Maximum'])),
                    findfirst=True)

    low = find_gap(hist, median,
                   math.trunc(float(hist['Minimum'])),
                   findfirst=True)

    highdist = high - median
    lowdist = median - low

    maskmax = find_gap(hist, median, (median + (2 * highdist)))
    maskmin = find_gap(hist, median, (median - (2 * lowdist)))

    util.log(isis.mask(in_path, to=out_path, minimum=maskmin,
                       maximum=maskmax).args)

    if not keep:
        hist_p.unlink()

    return


def get_unflip_thresh(hist: list, far, near, lowlimit) -> int:
    """Provides a robust threshhold for the unflipping process."""
    try:
        thresh = find_gap(hist, far, near)
    except ValueError:
        logging.info('No zero threshold found.')
        if d >= lowlimit:
            thresh = find_min_dn(hist, far, near)
            logging.info(f'Found a minimum threshold: {thresh}')
            if(thresh == far or thresh == near):
                raise ValueError(f'Minimum, {thresh}, at edge of range.')
        else:
            raise ValueError(f'Below {lowlimit}, skipping.')
    return thresh


def unflip(in_p: Path, out_p: Path, keep=False):
    """Attempt to indentify DNs whose bits have been flipped in the
    ISIS cube indicated by *in_p*, and unflip them.
    """
    to_del = isis.PathSet()

    # This full suite of deltas works well for the reverse-clock area
    # and even 'dark' images, but not 'real' images.
    # deltas = (8192, 4096, 2048, 1024, 512, 256, 128, 64)

    # Restricting the number of deltas might help, but this seems
    # arbitrary.
    deltas = (8192, 4096, 2048, 1024)

    count = 0
    suffix = '.bf{}-{}{}.cub'
    this_p = to_del.add(in_p.with_suffix(suffix.format(count, 0, 0)))
    this_p.symlink_to(in_p)

    median = math.trunc(float(isis.stats_k(in_p)['Median']))

    for (sign, pm, extrema) in ((+1, 'm', 'Maximum'),
                                (-1, 'p', 'Minimum')):
        # logging.info(pm)
        for delt in deltas:
            d = sign * delt
            far = median + d
            near = median + (d / 2)

            try:
                hist_p = to_del.add(this_p.with_suffix('.hist'))
                hist = histogram(this_p, hist_p)
            except ValueError:
                # Already have this .hist, don't need to remake.
                pass

            logging.info(f'bitflip position {pm}{delt}, near: {near} '
                         f'far: {far}, extrema: {hist[extrema]}')
            if((sign > 0 and far < float(hist[extrema])) or
               (sign < 0 and far > float(hist[extrema]))):
                count += 1
                s = suffix.format(count, pm, delt)
                next_p = to_del.add(this_p.with_suffix('').with_suffix(s))
                try:
                    thresh = get_unflip_thresh(hist, far, near, d)
                except ValueError as err:
                    logging.info(err)
                    count -= 1
                    break
                subtract_over_thresh(this_p, next_p, thresh, d, keep=keep)
                this_p = next_p
            else:
                logging.info("The far value was beyond the extrema. "
                             "Didn't bother.")

    shutil.move(this_p, out_p)

    if not keep:
        to_del.remove(this_p)
        to_del.unlink()

    return


if __name__ == "__main__":
    main()

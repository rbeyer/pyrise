#!/usr/bin/env python
"""HiJitReg registers color CCDs to corresponding red CCDs by using the ISIS tool
   hijitreg to perform a deconvolution of jittered image data."""

# Copyright 2019, Ross A. Beyer (rbeyer@seti.org)
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


# This program is based on HiColor version 1.99 2017/10/10
# and on the Perl HiColorInit program: ($Revision: 1.37 $ $Date: 2011/01/31 20:10:26 $)
# by Guy McArthur
# which is Copyright(C) 2007 Arizona Board of Regents, under the GNU GPL.
#
# This program is based on JitStats.pm ($Revision: 1.16 $ $Date: 2015/07/28 18:39:17 $)
# by Guy McArthur
# which is Copyright(C) 2015 Arizona Board of Regents, under the GNU GPL.
#
# Since that suite of software is under the GPL, none of it can be directly
# incorporated in this program, since I wish to distribute this software
# under the Apache 2 license.  Elements of this software (written in an entirely
# different language) are based on that software but rewritten from scratch to
# emulate functionality.

import argparse
import collections
import copy
import csv
import itertools
import logging
import math
import os
import re
import statistics
import sys
from datetime import datetime
from pathlib import Path

import pvl

import kalasiris as isis
import PyRISE.hirise as hirise
import PyRISE.util as util
import PyRISE.HiColorInit as hicolor


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     parents=[util.parent_parser()])
    parser.add_argument('-c', '--conf',    required=False,
                        default=Path(__file__).resolve().parent.parent /
                        'resources' / 'HiJitReg.conf')
    parser.add_argument('cubes', metavar="balance.cub and balance.precolor.cub files",
                        nargs='+')

    args = parser.parse_args()

    util.set_logging(args.log)

    conf = pvl.load(str(args.conf))

    cubes = list(map(hicolor.HiColorCube, args.cubes))
    (red4, red5, ir10, ir11, bg12, bg13) = hicolor.separate_ccds(cubes)

    try:
        successful_ccds = HiJitReg(red4, red5, ir10, ir11, bg12, bg13,
                                   conf, keep=args.keep)
    except RuntimeError as err:
        logging.critical('Unable to continue. ' + str(err))
        sys.exit()

    print('Successful CCDs are:')
    for c in successful_ccds:
        print('\t{}'.format(str(c)))
    return


class JitterCube(hicolor.HiColorCube, collections.abc.MutableMapping):
    '''A class for collecting and analyzing jitter statistics.'''

    def __init__(self, arg, config=Path(__file__).resolve().parent.parent / 'resources' / 'HiJitReg.conf'):
        if isinstance(arg, hicolor.HiColorCube):
            super().__init__(arg.path)
        else:
            super().__init__(arg)

        # self = copy.deepcopy(hi_color_cube)
        self.dictionary = dict()
        # HiColorCube isn't a dictionary, but we'll give access to its
        #   members as if it were:
        self.dictionary['bin'] = self.bin
        self.dictionary['tdi'] = self.tdi
        self.dictionary['lines'] = self.lines
        self.dictionary['samps'] = self.samps

        self.dictionary['CanSlither'] = False

        self.IgnoredPoints = set()

        # Just assuming that all of these will be in the self.dictionary
        # self.RegisterCount = None
        # self.AvgSampleOffset = None
        # self.AvgLineOffset = None
        # self.STDSampleOffset = None
        # self.STDLineOffset = None
        # self.SuspectCount = None
        # self.MatchedCount = None
        # self.RegisterCount = None
        # self.SearchSamples = None
        # self.SearchLines = None
        # self.EdgyCount = None
        # self.MatchedLineCount = None
        # self.Tolerance = None
        # self.Columns = None
        # self.Rows = None
        # self.canSlither = None
        # self.PatternSamples = None
        # self.PatternLines = None
        # self.SearchSamples = None
        # self.SearchLines = None

        if isinstance(config, (Path, str)):
            self.conf = pvl.load(str(config))
        elif isinstance(config, dict):
            self.conf = config
        else:
            raise Exception

        self.dictionary['ExcludeLimit'] = self.conf['Smoothing']['Exclude_Limit']
        self.dictionary['BadnessLimit'] = self.conf['Smoothing']['Badness_Limit']
        self.dictionary['BoxcarLength'] = self.conf['Smoothing']['Boxcar_Length']

        self.cnet_path = self.get_cnet_path(self)
        self.regdef_path = self.get_regdef_path(self)
        self.flattab_path = self.get_flattab_path(self)

    def __getitem__(self, key):
        return self.dictionary[key]

    def __setitem__(self, key, value):
        self.dictionary[key] = value

    def __delitem__(self, key):
        del self.dictionary[key]

    def __iter__(self):
        return iter(self.dictionary)

    def __len__(self):
        return len(self.dictionary)

    @staticmethod
    def get_pair_name(cube):
        pair_name = '{}_{}-{}'.format(str(cube.get_obsid()),
                                      hicolor.CCD_Corresponence[cube.get_ccd()],
                                      cube.get_ccd())
        return pair_name

    @staticmethod
    def _get_path(cube, suffix):
        pair = JitterCube.get_pair_name(cube)
        return cube.path.parent / (pair + suffix)

    @staticmethod
    def get_cnet_path(cube):
        return JitterCube._get_path(cube, '.control.pvl')

    @staticmethod
    def get_regdef_path(cube):
        return JitterCube._get_path(cube, '.regdef.pvl')

    @staticmethod
    def get_flattab_path(cube):
        return JitterCube._get_path(cube, '.flat.tab')

    def reset(self):
        self.IgnoredPoints.clear()
        self.parseRegDefs(self.regdef_path)
        self.parseFlatTab(self.flattab_path)
        self.parseCNetPVL(self.cnet_path)

    def parseRegDefs(self, path=None):
        '''Parse the register definition file to obtain the search and pattern
        sizes.'''
        if path is None:
            path = self.regdef_path

        p = pvl.load(str(path))
        self['PatternSamples'] = p['AutoRegistration']['PatternChip']['Samples']
        self['PatternLines'] = p['AutoRegistration']['PatternChip']['Lines']
        self['SearchSamples'] = p['AutoRegistration']['SearchChip']['Samples']
        self['SearchLines'] = p['AutoRegistration']['SearchChip']['Lines']
        return

    def parseFlatTab(self, path=None):
        '''Parses the flat file to obtain jitter registration result
        statistics.'''
        if path is None:
            path = self.flattab_path

        with open(path, 'r') as f:
            flat = f.read()

            match = re.search(r'#\s+Line Spacing:\s+(\S+)', flat)
            self['LineSpacing'] = float(match.group(1))

            match = re.search(r'#\s+Columns, Rows:\s+(\d+)\s+(\d+)', flat)
            self['Columns'] = int(match.group(1))
            self['Rows'] = int(match.group(2))

            match = re.search(r'#\s+Corr. Tolerance:\s+(\S+)', flat)
            self['Tolerance'] = float(match.group(1))

            match = re.search(r'#\s+Total Registers:\s+(\d+) of (\S+)', flat)
            self['MatchedCount'] = int(match.group(1))
            self['RegisterCount'] = int(match.group(2))

            match = re.search(r'#\s+Number Suspect:\s+(\S+)', flat)
            self['SuspectCount'] = int(match.group(1))

            match = re.search(r'#\s+Average Sample Offset:\s+(\S+)\s+StdDev:\s+(\S+)', flat)
            self['AvgSampleOffset'] = float(match.group(1))
            self['STDSampleOffset'] = float(match.group(2))

            match = re.search(r'#\s+Average Line Offset:\s+(\S+)\s+StdDev:\s+(\S+)', flat)
            self['AvgLineOffset'] = float(match.group(1))
            self['STDLineOffset'] = float(match.group(2))

            dialect = csv.Dialect
            dialect.delimiter = ' '
            dialect.skipinitialspace = True
            dialect.quoting = csv.QUOTE_NONE
            dialect.lineterminator = '\n'

            reader = csv.DictReader(itertools.filterfalse(lambda x:
                                                          x.startswith('#') or
                                                          x.isspace() or
                                                          len(x) == 0,
                                                          flat.splitlines()),
                                    dialect=dialect)

            if 'EdgyCount' not in self:
                self['EdgyCount'] = 0

            lineCount = 0
            for row in reader:
                # how many pixels in x is the edge of the pattern box from the reg point
                deltaSamp = abs(float(row['RegSamp']) - int(row['MatchSamp'])) + self['PatternSamples'] / 2
                # how many pixels in y is the edge of the pattern box from the reg point
                deltaLine = abs(float(row['RegLine']) - int(row['MatchLine'])) + self['PatternLines'] / 2

                # if the edge of the pattern box is more than two pixels away from
                # the search box, increment the count of marginal control points
                if((deltaSamp > (self['SearchSamples'] / 2 - 2)) or
                   (deltaLine > (self['SearchLines'] / 2 - 2))):
                    self['EdgyCount'] += 1

                    logging.info('Marginal register {} lines, {} samples to '
                                 'edge.'.format(deltaLine - self['SearchLines'] / 2,
                                                deltaSamp - self['SearchSamples'] / 2))
                lineCount += 1
            self['MatchedLineCount'] = lineCount
        return

    def parseCNetPVL(self, path=None):
        '''Parses the control net output from hijitreg, performs smoothing,
           and sets the array of ignorable points based on smoothing and
           badness ("goodness of fit").'''

        if path is None:
            path = self.cnet_path

        p = pvl.load(str(path))

        count = [0] * self['Columns']

        self.control_measures = self._get_control_measures(p)

        lineCount = 0
        for i, cm in enumerate(self.control_measures):
            offset = int(i - self['BoxcarLength'] / 2)
            length = int(self['BoxcarLength'])

            if offset < 0:
                offset = 0
                length = int(self['BoxcarLength'] / 2 + i)

            if self['BoxcarLength'] > (len(self.control_measures) - i):
                # offset not changed
                length = int(self['BoxcarLength'] / 2 + (len(self.control_measures)
                                                         - i))

            boxcar = map(lambda x: x['ErrorMagnitude'],
                         self.control_measures[offset:offset + length])

            median = statistics.median(boxcar)
            delta = abs(cm['ErrorMagnitude'] - median)

            if(cm['GoodnessOfFit'] > self['BadnessLimit'] or
               delta > self['ExcludeLimit']):
                self['MatchedCount'] -= 1
                self.IgnoredPoints.add(cm['PointId'])
                logging.info('Ignorable point {} with '.format(cm['PointId']) +
                             'badness {} and '.format(cm['GoodnessOfFit']) +
                             f'smoothing delta {delta}')
            else:
                if 'Row' in cm:
                    lineCount += 1

                if 'Column' in cm:
                    count[cm['Column']] += 1

            if len(tuple(filter(lambda x: x > 3, count))) >= 3:
                self['CanSlither'] = True
        self['MatchedLineCount'] = lineCount
        return

    @staticmethod
    def _get_control_measures(pvl) -> list:
        control_measures = list()

        # Original Perl issue: there were two "conditions" for
        # extracting information, one, labeled "<3.4" was to find
        # a ControlMeasure with a Reference = False key.  The other
        # labeled ">=3.4" was a ControlMeasure with MeasureType =
        # Candidate.  However, this condition really just ended
        # the line-by-line parsing, because the "Candidate"
        # ControlMeasure was the second one in the ControlPoint.
        # The proper logic is to get information from the
        # ControlMeasure that meets the conditions as implemented
        # below.

        for cp in pvl['ControlNetwork'].getlist('ControlPoint'):
            if('PointId' not in cp or
               'ControlMeasure' not in cp):
                continue
            for cm in cp.getlist('ControlMeasure'):
                if('MeasureType' not in cm or
                   # 'Reference' not in cm or
                   'GoodnessOfFit' not in cm or
                   'LineResidual' not in cm or
                   'SampleResidual' not in cm):
                    continue

                if cm['MeasureType'] == 'RegisteredPixel':
                    cm['ErrorMagnitude'] = math.hypot(cm['SampleResidual'].value,
                                                      cm['LineResidual'].value)
                    # Tack on a few extra values here, and then append
                    cm['PointId'] = cp['PointId']
                    match = re.search(r'Row\s+(\d+)\s+Column\s+(\d+)', cp['PointId'])
                    if match:
                        cm['Row'] = int(match.group(1))
                        cm['Column'] = int(match.group(2))

                    control_measures.append(cm)
        return control_measures

    def filterCNetPVL(self, path=None):
        '''Filters the CNET file and adds Ignored point information.'''

        if len(self.IgnoredPoints) == 0:
            return

        if path is None:
            path = self.cnet_path

        p = pvl.load(str(path))

        cn = pvl.PVLModule()

        badness = 0
        for (k, v) in p['ControlNetwork'].items():
            if k == 'ControlPoint':
                if v['PointId'] in self.IgnoredPoints and 'Ignore' not in v.keys():
                    v.append('Ignore', True)
                    badness += 1
                    logging.info('Ignoring point {}'.format(v['PointId']))
            cn.append(k, v)

        logging.info(f'{badness} point(s) ignored.')

        new_pvl = pvl.PVLModule(ControlNetwork=cn)

        with open(path, 'w') as stream:
            pvl.dump(new_pvl, stream)


def HiJitReg(red4, red5, ir10, ir11, bg12, bg13, conf: dict, keep=False) -> list:
    successful_ccds = list()
    if red4 is not None:
        for c in [ir10, bg12]:
            if c is not None:
                if jitter_iter(red4, c, conf, keep=keep):
                    successful_ccds.append(c)
    if red5 is not None:
        for c in [ir11, bg13]:
            if c is not None:
                if jitter_iter(red5, c, conf, keep=keep):
                    successful_ccds.append(c)

    # Not going to check to make sure that at most one pair fails.

    if bg12 not in successful_ccds and bg13 not in successful_ccds:
        raise RuntimeError('Registration failed for both BG halves.')

    return successful_ccds


def jitter_iter(red: hicolor.HiColorCube, color: hicolor.HiColorCube, conf: dict,
                keep=False) -> bool:
    '''Iterates through hijitreg for the color cube.'''

    temp_token = datetime.now().strftime('HiJitReg-%y%m%d%H%M%S')

    bin_ratio = color.bin / red.bin

    jit_param = dict()
    jit_param['COLS'] = conf['AutoRegistration']['ControlNet']['Control_Cols']
    jit_param['ROWS'] = conf['AutoRegistration']['ControlNet']['Control_Lines']
    jit_param['TOLERANCE'] = conf['AutoRegistration']['Algorithm']['Tolerance']
    jit_param['PATTERN_SAMPLES'] = conf['AutoRegistration']['PatternChip']['Samples']
    jit_param['PATTERN_LINES'] = conf['AutoRegistration']['PatternChip']['Lines']
    jit_param['SEARCH_SAMPLES'] = conf['AutoRegistration']['SearchChip']['Samples']
    jit_param['SEARCH_LINES'] = conf['AutoRegistration']['SearchChip']['Lines']
    jit_param['SEARCHLONGER_SAMPLES'] = conf['AutoRegistration']['SearchLongerChip']['Samples']
    jit_param['SEARCHLONGER_LINES'] = conf['AutoRegistration']['SearchLongerChip']['Lines']

    if bin_ratio > 3:
        hit_param['TOLERANCE'] -= conf['AutoRegistration']['Algorithm']['INCREMENT']

    channels = isis.getkey_k(color.path, 'Instrument', 'StitchedProductIds')

    coverage = 1.0

    if len(channels) < 2:
        coverage /= 2
        jit_param['COLS'] += jit_param['COLS'] / 2

    # A two-step process with completely different outcomes at each step, so we
    # can't really make a loop.
    step = 1

    logging.info(f'Attempting hijitreg iteration #{step} for {color}')

    color_jitter = JitterCube(color, conf)

    run_HiJitReg(red, color_jitter, jit_param, temp_token, keep=keep)

    ret = Analyze_Flat(color_jitter, step, coverage)

    if ret == -1:
        # edgy or suspect points only
        if jit_param['SEARCH_LINES'] == jit_param['SEARCHLONGER_LINES']:
            return True
        else:
            # use larger search box for all subsequent iterations (other CCDs too)
            jit_param['SEARCH_SAMPLES'] = jit_param['SEARCHLONGER_SAMPLES']
            jit_param['SEARCH_LINES'] = jit_param['SEARCHLONGER_LINES']
    elif ret == 0:
        # not enough points found
        # increase grid density
        jit_param['ROWS'] = jit_param['ROWS'] * 2
        if len(channels) >= 2:
            jit_param['COLS'] += 2
            coverage /= 2
    else:
        return True

    step += 1
    logging.info(f'Attempting hijitreg iteration #{step} for {color}')

    # second pass
    run_HiJitReg(red, color_jitter, jit_param, temp_token, keep=keep)

    # analyze output again
    ret = Analyze_Flat(color_jitter, step, coverage)

    if ret == 0:
        logging.info(f'Jitter registration failed for {color}')
        return False
    elif ret < 0:
        logging.info('!!! Validation Required !!!')
        return True
    else:
        return True


def run_HiJitReg(red: hicolor.HiColorCube, color: JitterCube, params: dict,
                 temptoken: str, keep=False):
    '''Examine output of control net and/or flat file to automatically remove
       out-of-bound points.'''

    file_status = 'OVERWRITE'
    if color.regdef_path.exists():
        file_status = pvl.load(str(color.regdef_path))['AutoRegistration']['HiJitReg']['File_Status']

    if file_status == 'KEEP':
        logging.info('Using existing regdef file due to KEEP file status.')
    else:
        logging.info(f'Writing new regdef file {color.regdef_path}')
        logging.info(params)
        write_regdef(color.regdef_path, params)

    tmp_control = color.cnet_path.with_suffix('.net')
    logging.info(isis.hijitreg(red.path, match=color.path,
                               regdef=color.regdef_path,
                               rows=params['ROWS'], columns=params['COLS'],
                               flat=color.flattab_path,
                               cnet=tmp_control).args)
    logging.info(isis.cnetbin2pvl(tmp_control, to=color.cnet_path).args)
    if not keep:
        tmp_control.unlink()
    return


def write_regdef(out_path: os.PathLike, parameters: dict):
    '''Writes PVL file that will be given to HiJitReg.'''
    out_p = Path(out_path)

    pvl_text = """Object = AutoRegistration

  Version = 2

  Group = HiJitReg
     File_Status  = "OVERWRITE"
     Control_Cols = {COLS}
     Control_Rows = {ROWS}
  End_Group

  Group = Algorithm
    Name      = MaximumCorrelation
    Tolerance = {TOLERANCE}
  End_Group

  Group = PatternChip
    Samples = {PATTERN_SAMPLES}
    Lines   = {PATTERN_LINES}
  End_Group

  Group = SearchChip
    Samples = {SEARCH_SAMPLES}
    Lines   = {SEARCH_LINES}
  End_Group

End_Object

"""

    out_p.write_text(pvl_text.format(**parameters))
    return


def Analyze_Flat(cube: JitterCube, step: int, fraction: float) -> int:
    cube.reset()
    cube.filterCNetPVL()

    logging.info('Matched Registers     = {} of {}'.format(cube['MatchedCount'],
                                                           cube['RegisterCount']))
    logging.info('Average Sample Offset = {}'.format(cube['AvgSampleOffset']))
    logging.info('Average Line Offset   = {}'.format(cube['AvgLineOffset']))
    logging.info('Edgy Count            = {}'.format(cube['EdgyCount']))
    logging.info('Suspect Points        = {}'.format(cube['SuspectCount']))

    if cube['AvgSampleOffset'] is None or cube['AvgLineOffset'] is None:
        logging.warn('No points met the correlation tolerance.')
        return 0

    if cube['CanSlither'] is False:
        logging.warn('Too few correlated lines found for cubic slither fit.')
        return 0

    good_fraction = (cube['MatchedCount'] - cube['SuspectCount']) / cube['RegisterCount']

    if good_fraction < 0.5 * fraction and step <= 1:
        logging.info(f'Too few correlated points ({good_fraction}) found at this tolerance')
        return 0

    elif good_fraction < 0.25 * fraction and step <= 2:
        logging.info(f'Too few correlated points ({good_fraction}) found at this tolerance')
        return -1

    if cube['EdgyCount'] > 2 and good_fraction > 0.8 * fraction:
        logging.info('More than two edgy points with search box size.')
        return -1

    if cube['SuspectCount'] > 3:
        logging.info('More than three suspect points with search box size.')
        return -1

    return 1

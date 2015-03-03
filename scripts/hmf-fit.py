'''
Created on 27/02/2015

@author: Steven Murray
'''

#!/Users/Steven/anaconda/bin/python2.7
# encoding: utf-8
'''
halomod-fit -- fit a model to data

halomod-fit is a script for fitting arbitrary Halo Model quantities to given
data. For instance, it makes an MCMC fit to the projected correlation function
of galaxies a simple procedure. A config file is necessary to run the application. 
'''

import sys
import os
import traceback

from argparse import ArgumentParser
from argparse import RawDescriptionHelpFormatter
from ConfigParser import SafeConfigParser as cfg
cfg.optionxform = str
import numpy as np
from hmf import fit
import json
import time
import errno
from os.path import join
from numbers import Number
import pickle
from emcee import autocorr
from importlib import import_module

__all__ = []
__version__ = 0.1
__date__ = '2014-05-14'
__updated__ = '2014-05-14'

DEBUG = 0
TESTRUN = 0
PROFILE = 0

class CLIError(Exception):
    '''Generic exception to raise and log different fatal errors.'''
    def __init__(self, msg):
        super(CLIError).__init__(type(self))
        self.msg = "E: %s" % msg
    def __str__(self):
        return self.msg
    def __unicode__(self):
        return self.msg

def main(argv=None):
    '''Command line options.'''

    if argv is None:
        argv = sys.argv
    else:
        sys.argv.extend(argv)

    program_name = os.path.basename(sys.argv[0])
    program_version = "v%s" % __version__
    program_build_date = str(__updated__)
    program_version_message = '%%(prog)s %s (%s)' % (program_version, program_build_date)
    program_shortdesc = __import__('__main__').__doc__.split("\n")[1]
    program_license = '''%s

  Created by Steven Murray on %s.
  Copyright 2013 organization_name. All rights reserved.

  Licensed under the Apache License 2.0
  http://www.apache.org/licenses/LICENSE-2.0

  Distributed on an "AS IS" basis without warranties
  or conditions of any kind, either express or implied.

USAGE
''' % (program_shortdesc, str(__date__))

    try:
        # Setup argument parser
        parser = ArgumentParser(description=program_license, formatter_class=RawDescriptionHelpFormatter)
        parser.add_argument("-v", "--verbose", dest="verbose", action="count", help="set verbosity level [default: %(default)s]")
        parser.add_argument('-V', '--version', action='version', version=program_version_message)

        parser.add_argument("conf", help="config file")
        parser.add_argument("-p", "--prefix", default="", help="an optional prefix for the output files.")
        parser.add_argument("-r", "--restart", default=False, action="store_true", help="restart (continue) from file")

        # Process arguments
        args = parser.parse_args()

        ### READ CONFIG FILE ###
        options = read_config(args.conf)

        if options["IO"]["outdir"]:
            try:
                os.makedirs(options["IO"]["outdir"])
            except OSError, e:
                if e.errno != errno.EEXIST:
                    raise

        x, y, sigma = get_data(**options["Data"])

        # Get params that are part of a dict (eg. HOD)
        dict_p = {k:options[k] for k in options if k.endswith("Params")}
        priors, keys, guess = param_setup(**dict_p)

        quantity = options["RunOptions"]["quantity"]
        blobs = json.loads(options["RunOptions"]["der_params"])
        framework = options["RunOptions"]["framework"]
        if args.restart:
            initial, prev_samples = get_initial(args.prefix, int(options["MCMC"]["nwalkers"]))
        else:
            initial, prev_samples = None, 0

        run(x, y, quantity, blobs, sigma, priors, keys, guess, options,
            args.prefix, initial, prev_samples, framework)

        return 0
    except KeyboardInterrupt:
        ### handle keyboard interrupt ###
        return 0
    except Exception, e:
        if DEBUG or TESTRUN:
            raise e
        traceback.print_exc()
        indent = len(program_name) * " "
        sys.stderr.write(program_name + ": " + repr(e) + "\n")
        sys.stderr.write(indent + "  for help use --help\n")
        return 2

def get_data(**kwargs):
    """
    Import the data to be compared to (both data and var/covar)
    
    Returns
    -------
    float array:
        array of x values.
        
    float array:
        array of y values.
        
    float array or None:
        Standard Deviation of y values or None if covariance is provided
        
    float array or None:
        Covariance of y values, or None if not provided.
    """
    data = np.genfromtxt(**kwargs['data_file'])

    x = data[:, 0]
    y = data[:, 1]
    try:
        sd = data[:, 2]
    except IndexError:
        sd = None

    try:
        cov = np.genfromtxt(**kwargs["cov_file"])
    except:
        cov = None

    if sd is None and cov is None:
        raise ValueError("""
Either a univariate standard deviation, or multivariate cov matrix must be provided.
""")

    return x, y, sd or cov

#===============================================================================
# PARAMETER SETUP
#===============================================================================
def param_setup(**params):
    """
    Takes a dictionary of input parameters, with keys defining the parameters
    and the values defining various aspects of the priors, and converts them
    to useable Prior() instances, along with keys and guesses.
    
    Note that here, *only* cosmological parameters are able to be set as 
    multivariate normal priors (this is not true in general, but for the CLI 
    it is much simpler). All other parameters may be set as Normal or Uniform
    priors. 

    Returns
    -------
    priors : list
        A list of Prior() classes corresponding to each parameter specified. 
        Names in these will be prefixed by "<dict>:" for parameters required
        to pass to dictionaries.
        
    keys : list
        A list of of parameter names (without prefixes)
        
    guess : list
        A list containing an initial guess for each parameter.
    """
    # Set-up returned lists of parameters
    priors = []
    guess = []
    keys = []

    # Get covariance data for the cosmology (ie. name of CMB mission if provided)
    covdata = params["cosmo_paramsParams"].pop("covar_data", None)
    if covdata:
        try:
            cosmo_cov = getattr(sys.modules["hmf.fit"], covdata)
        except AttributeError:
            raise AttributeError("%s is not a valid cosmology dataset" % covdata)
        except Exception:
            raise

    # Deal specifically with cosmology priors, separating types
    cosmo_priors = {k:json.loads(v) for k, v in params["cosmo_paramsParams"].iteritems()}
    # the following rely on covdata
    cov_vars = {k:v for k, v in cosmo_priors.iteritems() if v[0] == "cov"}
    norm_vars = {k:v for k, v in cosmo_priors.iteritems() if (v[0] == "norm" and len(v) == 2)}
    # remove these to be left with normal stuff
    for k in cov_vars.keys() + norm_vars.keys():
        del params["cosmo_paramsParams"][k]

    if cov_vars:
        priors += cosmo_cov.get_cov_prior(*cov_vars)
    if norm_vars:
        priors += cosmo_cov.get_normal_priors(*norm_vars)

    # All non-cosmology-covariance-dependent stuff that is top-level
    otherparams = params.pop("OtherParams")
    for param, val in otherparams.iteritems():
        priors += set_prior(param, val)

    # All non-cosmology-covariance-dependent stuff that is nested
    for k, v in params.iteritems():
        for kk, vv in v.iteritems():
            priors += set_prior(k[:-6] + ":" + kk, vv)

    # Create list of all the names of parameters (pure name without :)
    for prior in priors:
        if isinstance(prior.name, basestring):
            keys += [prior.name]
        else:
            keys += prior.name
    keys = [k.split(":")[-1] for k in keys]

    # Get the guesses
    guess = []
    for i, k in enumerate(keys):
        val = json.loads(allparams[k])
        if val[-1] is None:
            guess.append(priors[i].guess(k))
        else:
            guess.append(val[-1])


    print "KEY NAMES: ", keys
    print "INITIAL GUESSES: ", guess

    return priors, keys, guess

def set_prior(param, val):
    val = json.loads(val)
    if val[0] == 'unif':
        return [mc.Uniform(param, val[1], val[2])]
    elif val[0] == 'norm':
        return [mc.Normal(param, val[1], val[2])]
    elif val[0] == "log":
        return [mc.Log(param, val[1], val[2])]



def import_class(cl):
    d = cl.rfind(".")
    classname = cl[d + 1:len(cl)]
    m = __import__(cl[0:d], globals(), locals(), [classname])
    return getattr(m, classname)

def run(x, y, quantity, blobs, sigma, priors, keys, guess, options, prefix,
        initial, prev_samples, framework):

    if prefix:
        if not prefix.endswith("."):
            prefix += "."

    kwargs = options["Model"]
    for k in kwargs:
        try:
            kwargs[k] = json.loads(kwargs[k])
        except:
            pass

    nwalkers = int(options["MCMC"]["nwalkers"])
    nsamples = int(options["MCMC"]["nsamples"])
    burnin = json.loads(options["MCMC"]["burnin"])
    nthreads = int(options["RunOptions"]["nthreads"])
    chunks = int(options["IO"]["chunks"])
    relax = bool(options["RunOptions"]["relax"])
    store_class = bool(options["RunOptions"]["store_class"])

    F = import_class(framework)
    instance = F(**kwargs)

    # pre-get the quantity
    getattr(instance, quantity)

    prefix = join(options["IO"]["outdir"], prefix)

    # start = time.time()
    fitter = fit.MCMC(priors=priors, data=y, quantity=quantity, sigma=sigma,
                 guess=guess, blobs=blobs, verbose=verbose,
                 store_class=store_class, relax=relax)

    s = fitter.fit(F, nwalkers=nwalkers, nsamples=nsamples, burnin=burnin,
                 nthreads=nthreads, prefix=prefix, chunks=chunks,
                 initial_pos=initial)

    # Grab acceptance fraction from initial run if possible
    new_accepted = np.mean(s.acceptance_fraction) * nsamples * nwalkers
    if initial is not None:
        try:
            with open(prefix + "log", 'r') as f:
                for line in f:
                    if line.startswith("Acceptance Fraction:"):
                        af = float(line[20:])
                        naccepted = af * prev_samples
        except IOError:
            naccepted = 0
            prev_samples = 0
    else:
        naccepted = 0
    acceptance = (naccepted + new_accepted) / (prev_samples + nwalkers * nsamples)

    # Ditch the sampler from memory
    del s

    # Read in total chain
    chain = np.genfromtxt(prefix + "chain").reshape((nwalkers, nsamples, -1))
    acorr = autocorr.integrated_time(np.mean(chain, axis=0), axis=0,
                                     window=50, fast=False)
    chain = chain.reshape((nwalkers * nsamples, -1))
    # Write out the logfile
    with open(prefix + "log", 'w') as f:
        if isinstance(burnin, int):
            f.write("Average time: %s\n" % ((time.time() - start) / (nwalkers * nsamples + nwalkers * burnin)))
        else:
            f.write("Average time (discounting burnin): %s\n" % ((time.time() - start) / (nwalkers * nsamples)))
        f.write("Nsamples:  %s\n" % nsamples)
        f.write("Nwalkers: %s\n" % nwalkers)
        f.write("Mean values = %s\n" % np.mean(chain, axis=0))
        f.write("Covariance Matrix: %s\n" % np.cov(chain.T))
        f.write("Acceptance Fraction: %s\n" % acceptance)
        f.write("Acorr: %s\n" % json.dumps(acorr.tolist()))


#===============================================================================
# Get initial data if continuing from previous calculations
#===============================================================================
def get_initial(prefix, nwalkers):
    """
    Tries to find a chain in the current directory to use.
    """
    if prefix:
        if not prefix.endswith("."):
            prefix += "."

    try:
        x = np.genfromtxt(prefix + "chain")
        nsamples = x.shape[0]
        return x[-nwalkers:, :], nsamples
    except:
        warnings.warn("Problem importing old file, starting afresh")
        return None, 0

#===============================================================================
# READ CONFIG
#===============================================================================
def read_config(fname):
    config = cfg()
    config.read(fname)
    res = {s:dict(config.items(s)) for s in config.sections()}
    if "outdir" not in res["IO"]:
        res["IO"]["outdir"] = ""
    return res

if __name__ == "__main__":
    if DEBUG:
        sys.argv.append("-v")
    if TESTRUN:
        import doctest
        doctest.testmod()
    if PROFILE:
        import cProfile
        import pstats
        profile_filename = 'run_profile.txt'
        cProfile.run('main()', profile_filename)
        statsfile = open("profile_stats.txt", "wb")
        p = pstats.Stats(profile_filename, stream=statsfile)
        stats = p.strip_dirs().sort_stats('cumulative')
        stats.print_stats()
        statsfile.close()
        sys.exit(0)
    sys.exit(main())



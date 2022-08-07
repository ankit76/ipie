"""Routines and classes for estimation of observables."""

from __future__ import print_function

import copy
import os
import time
import warnings

import h5py
import numpy
import scipy.linalg

from mpi4py import MPI

from ipie.estimators.energy import EnergyEstimator
from ipie.estimators.estimator_base import EstimatorBase
from ipie.estimators.utils import H5EstimatorHelper
from ipie.utils.io import get_input_value

# Some supported (non-custom) estimators
_predefined_estimators = {
        'energy': EnergyEstimator,
        }


class EstimatorHandler(object):
    """Container for qmc options of observables.

    Parameters
    ----------
    comm : MPI.COMM_WORLD
        MPI Communicator
    system : :class:`ipie.hubbard.Hubbard` / system object in general.
        Container for model input options.
    trial : :class:`ipie.trial_wavefunction.X' object
        Trial wavefunction class.
    verbose : bool
        If true we print out additional setup information.
    options: dict
        input options detailing which estimators to calculate. By default only
        mixed options will be calculated.

    Attributes
    ----------
    estimators : dict
        Dictionary of estimator objects.
    """

    def __init__(
        self,
        comm,
        system,
        hamiltonian,
        trial,
        nsteps=1,
        options={},
        verbose=False
    ):
        if verbose:
            print("# Setting up estimator object.")
        if comm.rank == 0:
            self.index = get_input_value(options, "index", default=0, verbose=verbose)
            self.filename = get_input_value(options, "filename", default=None, verbose=verbose)
            self.basename = get_input_value(options, "basename", default="estimates", verbose=verbose)
            if self.filename is None:
                overwrite = get_input_value(options, "overwrite", default=False, verbose=verbose)
                self.filename = self.basename + ".%s.h5" % self.index
                while os.path.isfile(self.filename) and not overwrite:
                    self.index = int(self.filename.split(".")[1])
                    self.index = self.index + 1
                    self.filename = self.basename + ".%s.h5" % self.index
            with h5py.File(self.filename, "w") as fh5:
                pass
            if verbose:
                print("# Writing estimator data to {}.".format(self.filename))
        else:
            self.filename = None
        self.buffer_size = get_input_value(
                options,
                "buffer_size",
                default=1000,
                verbose=verbose)
        observables = get_input_value(
                options,
                "observables",
                default={"energy": {}},
                alias=["estimators", "observable"],
                verbose=verbose)
        self._estimators = {}
        self._shapes = []
        self._offsets = {}
        self._num_estim = 0
        self.nsteps = nsteps
        for obs, obs_dict in observables.items():
            try:
                est = _predefined_estimators[obs](
                            comm=comm,
                            system=system,
                            ham=hamiltonian,
                            trial=trial,
                            options=obs_dict,
                            nsteps=self.nsteps
                            )
                self.__setitem__(obs, est)
            except KeyError:
                raise RuntimeError(f"unknown observable: {obs}")
        if verbose:
            print("# Finished settting up estimator object.")

    def __setitem__(self, name: str, estimator: EstimatorBase) -> None:
        self._estimators[name] = estimator
        shape = estimator.shape
        self._shapes.append(estimator.shape)
        if len(self._offsets.keys()) == 0:
            self._offsets[name] = 0
            prev_obs = name
        else:
            prev_obs = list(self._offsets.keys())[-1]
            offset = numpy.prod(shape) + self._offsets[prev_obs]
            self._offsets[name] = offset

    def get_offset(self, name: str) -> int:
        offset = self._offsets.get(name)
        assert offset is not None, f"Unknown estimator name {name}"
        return offset

    def __getitem__(self, key):
        return self._estimators[key]

    @property
    def items(self):
        return self._estimators.items

    @property
    def size(self):
        return sum(numpy.prod(o.shape) for k, o in self._estimators.items())

    def initialize(self, comm):
        self.local_estimates = numpy.zeros((self.size),
                dtype=numpy.complex128)
        self.global_estimates = numpy.zeros((self.size),
                dtype=numpy.complex128)
        header = '{:>17s}  '.format('Block')
        for k, e in self.items():
            if e.print_to_stdout:
                header += e.header_to_text
        self.output = H5EstimatorHelper(self.filename,
                base="block_size_1",
                chunk_size=self.buffer_size,
                shape=(self.size,)
                )
        if comm.rank == 0:
            with h5py.File(self.filename, 'r+') as fh5:
                for k, o in self.items():
                    fh5[f'block_size_1/shape/{k}'] = o.shape
                    fh5[f'block_size_1/size/{k}'] = o.size
                    fh5[f'block_size_1/names/{k}'] = ' '.join(name for name in o.names)
                    fh5[f'block_size_1/offset/{k}'] = self.get_offset(k)
        if comm.rank == 0:
            print(header)


    def dump_metadata(self):
        with h5py.File(self.filename, "a") as fh5:
            fh5["metadata"] = self.json_string

    def increment_file_number(self):
        self.index = self.index + 1
        self.filename = self.basename + ".%s.h5" % self.index


    def compute_estimators(
        self, comm, system, hamiltonian, trial, walker_batch, istep
    ):
        """Update estimators with bached psi

        Parameters
        ----------
        """
        # Compute all estimators
        # For the moment only consider estimators compute per block.
        # TODO: generalize for different block groups (loop over groups)
        for k, e in self.items():
            e.compute_estimator(system, walker_batch, hamiltonian, trial, istep=istep)
            start = self.get_offset(k)
            end = start + self[k].size
            self.local_estimates[start:end] += e.data

    def print(self, comm, block, div_factor=None):
        comm.Reduce(self.local_estimates, self.global_estimates, op=MPI.SUM)
        output_string = ' '
        for k, e in self.items():
            if comm.rank == 0:
                start = self.get_offset(k)
                end = start + self[k].size
                est_data = self.global_estimates[start:end]
                e.post_reduce_hook(est_data, div_factor=div_factor)
                est_string = e.data_to_text(est_data)
                e.to_ascii_file(est_string)
                if e.print_to_stdout:
                    output_string += est_string
        if comm.rank == 0:
            self.output.push_to_chunk(
                    self.global_estimates,
                    f"data")
            self.output.increment()
        if comm.rank == 0:
            print(f"{block:>17d} " + output_string)
        self.zero()

    def zero(self):
        self.local_estimates[:] = 0.0
        self.global_estimates[:] = 0.0
        for k, e in self.items():
            e.zero()

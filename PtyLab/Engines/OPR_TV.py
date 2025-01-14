import numpy as np
from matplotlib import pyplot as plt

try:
    import cupy as cp
except ImportError:
    print('Cupy not available, will not be able to run GPU based computation')
    # Still define the name, we'll take care of it later but in this way it's still possible
    # to see that gPIE exists for example.
    cp = None

# fracPy imports
from PtyLab.Reconstruction.Reconstruction import Reconstruction
from PtyLab.Engines.BaseEngine import BaseEngine
from PtyLab.ExperimentalData.ExperimentalData import ExperimentalData
from PtyLab.Params.Params import Params
from PtyLab.utils.gpuUtils import getArrayModule, isGpuArray, asNumpyArray
from PtyLab.Monitor.Monitor import Monitor
from PtyLab.utils.fsvd import rsvd
import logging
import tqdm
import sys


class OPR_TV(BaseEngine):

    def __init__(self, reconstruction: Reconstruction, experimentalData: ExperimentalData, params: Params, monitor: Monitor):
        # This contains reconstruction parameters that are specific to the reconstruction
        # but not necessarily to ePIE reconstruction
        super().__init__(reconstruction, experimentalData, params, monitor)
        self.logger = logging.getLogger('ePIE')
        self.logger.info('Sucesfully created ePIE ePIE_engine')
        self.logger.info('Wavelength attribute: %s', self.reconstruction.wavelength)
        self.initializeReconstructionParams()

    def initializeReconstructionParams(self):
        """
        Set parameters that are specific to the ePIE settings.
        :return:
        """
        self.alpha = 0.05
        self.betaProbe = 0.25
        self.betaObject = 0.25
        self.numIterations = 50
        self.OPR_modes = self.params.OPR_modes
        self.n_subspace = self.params.n_subspace
        print(self.OPR_modes)

    def reconstruct(self):
        self._prepareReconstruction()

        # OPR parameters
        # self.OPR_modes = np.array([0, 1, 2, 3, 4])
        Nmodes = self.OPR_modes.shape[0]
        Np = self.reconstruction.Np
        Nframes = self.experimentalData.numFrames
        mode_slice = slice(np.min(self.OPR_modes), np.max(self.OPR_modes) + 1) 
        n_subspace = self.n_subspace 
        # n_subspace = 4

        self.reconstruction.probe_stack = cp.zeros((1, 1, Nmodes, 1, Np, Np, Nframes), dtype=cp.complex64)
        # what are you doing here? Shouldn't these be incoherent?
        for i in self.OPR_modes:
            self.reconstruction.probe_stack[0, 0, i, 0, :, :, :] = cp.repeat(self.reconstruction.probe[0, 0, i, 0, :, :, cp.newaxis], Nframes, axis=2)
        # faster way of achieving something similar, but I'm not sure that this is correct
        # self.reconstruction.probe_stack[0, 0, :, :, :, :] = self.reconstruction.probe[..., None]

        # actual reconstruction ePIE_engine
        self.pbar = tqdm.trange(self.numIterations, desc='OPR_TV', file=sys.stdout, leave=True)
        for loop in self.pbar:
            self.it = loop
            # set position order
            self.setPositionOrder()
            for positionLoop, positionIndex in enumerate(self.positionIndices):
                # get object patch
                row, col = self.reconstruction.positions[positionIndex]
                sy = slice(row, row + self.reconstruction.Np)
                sx = slice(col, col + self.reconstruction.Np)
                # note that object patch has size of probe array
                objectPatch = self.reconstruction.object[..., sy, sx].copy()

                # Get dim reduced probe
                self.reconstruction.probe[:, :, mode_slice, :, :, :] = self.reconstruction.probe_stack[..., positionIndex]

                # make exit surface wave
                # make exit surface wave
                self.reconstruction.esw = objectPatch * self.reconstruction.probe 
                
                # propagate to camera, intensityProjection, propagate back to object
                self.intensityProjection(positionIndex)

                # difference term
                DELTA = self.reconstruction.eswUpdate - self.reconstruction.esw
                
                tv_freq = 5
                tv = False 
                if loop % tv_freq == 0 and tv:
                    self.reconstruction.object[..., sy, sx] = self.objectPatchUpdate_TV(objectPatch, DELTA)
                else:
                    # object update
                    self.reconstruction.object[..., sy, sx] = self.objectPatchUpdate(objectPatch, DELTA)
                
                # probe update
                weight = 1
                if self.params.weigh_probe_updates_by_intensity:
                    weight = self.experimentalData.relative_intensity(positionIndex)
                self.reconstruction.probe = self.probeUpdate(objectPatch, DELTA, weight)

                # save first, dominant probe mode
                self.reconstruction.probe_stack[..., positionIndex] = cp.copy(self.reconstruction.probe[:, :, mode_slice, :, :, :]) 

            # get error metric
            self.getErrorMetrics()

            # added this line
            orthogonalize_modes = True 
            if orthogonalize_modes:
               self.orthogonalizeIncoherentModes()

            OPR_constraint = True
            if OPR_constraint:
               self.reconstruction.probe_stack = self.orthogonalizeProbeStack(self.reconstruction.probe_stack, n_subspace)

            # apply Constraints
            self.applyConstraints(loop)

            # show reconstruction
            self.showReconstruction(loop)

        if self.params.gpuFlag:
            self.logger.info('switch to cpu')
            self._move_data_to_cpu()
            self.params.gpuFlag = 0

    def orthogonalizeIncoherentModes(self):
        nFrames = self.experimentalData.numFrames 
        n = self.reconstruction.Np
        nModes = self.reconstruction.probe_stack.shape[2]
        for pos in range(nFrames):
            probe = self.reconstruction.probe_stack[0, 0, :, 0, :, :, pos]
            probe = probe.reshape(nModes, n*n)

            U, s, Vh = self.svd(probe)

            #modes = cp.dot(cp.diag(s), Vh).reshape(nModes, n, n)
            modes = (s[:,None]*Vh).reshape(nModes, n, n)
            self.reconstruction.probe_stack[0, 0, :, 0, :, :, pos] = modes

    def average(self, arr):
        arr_start = arr[:-1]
        arr_end = arr[1:]
        arr_end = cp.append(arr_end, 0)
        arr_start = cp.append(0, arr_start)
        divider = cp.ones_like(arr) * 3
        divider[0] = 2
        divider[-1] = 2
        return (arr + arr_end + arr_start) / divider

    def svd(self, P):
        if isGpuArray(P):
            try:
                return cp.linalg.svd(P, full_matrices=False)
            except:
                print('Something is wrong with SVD on cuda. Probably an installation error')
                raise
        A, v, At = np.linalg.svd(asNumpyArray(P), full_matrices=False)
        if isGpuArray(P):
            A = cp.array(A)
            v = cp.array(v)
            At = cp.array(At)
        return A, v, At

    def rsvd(self, P, n_dim):
        return  rsvd(P, n_dim)
        # A, v, At = self.svd(P)
        # v[n_dim:] = 0
        # return A, v, At

    def orthogonalizeProbeStack(self, probe_stack, n_dim):
        plot_cycle = 10
        plot = False
        n = self.reconstruction.Np
        nFrames = self.experimentalData.numFrames

        for i in self.OPR_modes:

            # U, s, Vh = cp.linalg.svd(probe_stack[:, :, i, :, :, :].reshape(n * n, nFrames), full_matrices=False)
            # U, s, Vh = self.svd(probe_stack[:, :, i, :, :, :].reshape(n * n, nFrames))
            # from cupyx.scipy.sparse.linalg import svds


            #s[n_dim:] = 0


            U, s, Vh = self.rsvd(probe_stack[:, :, i, :, :,:].reshape(n*n, nFrames), n_dim)


            # allow only slow changes
            neighbor_constraint = False
            if neighbor_constraint and self.it > 3 and self.it % 25 == 0:
                # Calculate the average of neigboring singular values
                content = cp.dot(cp.diag(s), Vh)
                for j in range(n_dim):
                    content[j] = self.average(content[j])

                probe_stack[:, :, i, :, :, :] = self.alpha * probe_stack[:, :, i, :, :, :] + (1 - self.alpha) * cp.dot(U, content).reshape(n, n, nFrames)
            else:
                # probe_stack[:, :, i, :, :, :] = self.alpha * probe_stack[:, :, i, :, :, :] + (1 - self.alpha) * cp.dot(U, cp.dot(cp.diag(s), Vh)).reshape(n, n, nFrames)
                update = (U@(s[:,None]*Vh)).reshape(n,n,nFrames)
                # probe_stack[:, :, i, :, :, :] = self.alpha * probe_stack[:, :, i, :, :, :] + (1 - self.alpha) * update
                probe_stack[:, :, i, :, :, :] *= self.alpha
                probe_stack[:, :, i, :, :, :] += (1 - self.alpha) * update

        if self.it == 0 and plot:
            plt.ion()
            self.fig = plt.figure('content')
            self.fig_2 = plt.figure('second modes')
            self.ax = self.fig.add_subplot(111)
            self.ax_2 = self.fig_2.add_subplot(121)
            self.ax_3 = self.fig_2.add_subplot(122)
      
        if self.it % plot_cycle == 0 and plot:
            print(U.shape)
            self.reconstruction.modes = U.reshape(n, n, nFrames)
            content = cp.dot(cp.diag(s), Vh)
            self.ax.imshow(np.log10(np.abs(content.get())[0:3, :]), aspect='auto')
            self.ax_2.imshow(np.log10(np.abs(self.reconstruction.modes[:, :, 1].get())))
            self.ax_3.imshow(np.log10(np.abs(self.reconstruction.modes[:, :, 2].get())))
            self.fig.canvas.draw() 

        return probe_stack 

    def objectPatchUpdate(self, objectPatch: np.ndarray, DELTA: np.ndarray):
        """
        Todo add docstring
        :param objectPatch:
        :param DELTA:
        :return:
        """
        # find out which array module to use, numpy or cupy (or other...)
        xp = getArrayModule(objectPatch)

        frac = self.reconstruction.probe.conj() / xp.max(xp.sum(xp.abs(self.reconstruction.probe) ** 2, axis=(0, 1, 2, 3)))
        return objectPatch + self.betaObject * xp.sum(frac * DELTA, axis=(0,2,3), keepdims=True)

    def probeUpdate(self, objectPatch: np.ndarray, DELTA: np.ndarray, weight: float, gimmel=0.1):
        """
        Todo add docstring
        :param objectPatch:
        :param DELTA:
        :return:
        """
        # find out which array module to use, numpy or cupy (or other...)
        xp = getArrayModule(objectPatch)
        frac = objectPatch.conj() / (xp.max(xp.sum(xp.abs(objectPatch) ** 2, axis=(0,1,2,3)))+gimmel)
        frac = frac * weight
        r = self.reconstruction.probe + self.betaProbe * xp.sum(frac * DELTA, axis=(0, 1, 3), keepdims=True)
        return r
    
    def probeUpdate_new(self, objectPatch: np.ndarray, DELTA: np.ndarray):
        """
        Todo add docstring
        :param objectPatch:
        :param DELTA:
        :return:
        """
        # find out which array module to use, numpy or cupy (or other...)
        xp = getArrayModule(objectPatch)
        # frac = objectPatch.conj() / xp.max(xp.sum(xp.abs(objectPatch) ** 2, axis=(0,1,2,3)))
        self.reconstruction.probe += self.betaProbe * xp.sum(objectPatch.conj() / xp.max(xp.sum(xp.abs(objectPatch) ** 2, axis=(0,1,2,3))) * DELTA, axis=(0, 1, 3), keepdims=True)


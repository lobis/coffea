from coffea.jetmet_tools.JECStack import JECStack
import awkward
import awkward1
import numpy as np
from collections import namedtuple
import warnings
from copy import copy
from functools import partial

from coffea.nanoevents.methods.nanoaod import Jet as Ak1Jet

_stack_parts = ['jec', 'junc', 'jer', 'jersf']
_MIN_JET_ENERGY = 1e-2


# we're gonna assume that the first record array we encounter is the flattened data
def rewrap_recordarray(layout, depth, data):
    if isinstance(layout, awkward1.layout.RecordArray):
        return lambda: data
    return None


def awkward1_rewrap(arr, like_what, gfunc):
    behavior = awkward1._util.behaviorof(like_what)
    func = partial(gfunc, data=arr.layout)
    layout = awkward1.operations.convert.to_layout(like_what)
    newlayout = awkward1._util.recursively_apply(layout, func)
    return awkward1._util.wrap(newlayout, behavior=behavior)


def rand_gauss_ak0(arr, var):
    item = arr[var]
    wrap, _ = awkward.util.unwrap_jagged(item,
                                         item.JaggedArray,
                                         (item, ))
    return wrap(np.random.normal(size=item.content.size))


def rand_gauss_ak1(arr, var):
    item = arr[var]

    def getfunction(layout, depth):
        if (isinstance(layout, awkward1.layout.NumpyArray)
            or not isinstance(layout, (awkward1.layout.Content,
                                       awkward1.partition.PartitionedArray)
           )):
            return lambda: awkward1.layout.NumpyArray(np.random.normal(size=len(layout)))
        return None

    out = awkward1._util.recursively_apply(
        awkward1.operations.convert.to_layout(item),
        getfunction)
    assert out is not None
    return awkward1._util.wrap(out, awkward1._util.behaviorof(item))


def jer_smear(arr, variation, forceStochastic, ptGenJet, ptJet, etaJet):
    pt_gen = arr[ptGenJet] if not forceStochastic else None
    jetPt = arr[ptJet]

    if isinstance(jetPt, awkward.array.base.AwkwardArray):
        if forceStochastic:
            wrap, arrays = awkward.util.unwrap_jagged(jetPt,
                                                      jetPt.JaggedArray,
                                                      (jetPt, ))
            pt_gen = wrap(np.zeros_like(arrays[0]))
    elif isinstance(jetPt, awkward1.highlevel.Array):
        def getfunction(layout, depth):
            if (isinstance(layout, awkward1.layout.NumpyArray)
                or not isinstance(layout, (awkward1.layout.Content,
                                           awkward1.partition.PartitionedArray)
               )):
                return lambda: awkward1.layout.NumpyArray(np.zeros_like(size=len(jetPt)))
            return None
        if forceStochastic:
            pt_gen = awkward1._util.recursively_apply(
                awkward1.operations.convert.to_layout(jetPt),
                getfunction)
            pt_gen = awkward1._util.wrap(pt_gen, awkward1._util.behaviorof(jetPt))
    else:
        raise Exception('\'arr\' must be an awkward array of some kind!')

    jersmear = arr['jet_energy_resolution'] * arr['jet_resolution_rand_gauss']
    jersf = arr['jet_energy_resolution_scale_factor'][:, variation]
    print('jersf', arr['jet_energy_resolution_scale_factor'])
    doHybrid = pt_gen > 0.

    detSmear = 1. + (jersf - 1.) * (-pt_gen + jetPt) / jetPt  # because of awkward1.0#367
    stochSmear = 1. + np.sqrt(np.maximum(jersf**2 - 1., 0.)) * jersmear

    min_jet_pt = _MIN_JET_ENERGY / np.cosh(arr[etaJet])
    min_jet_pt_corr = min_jet_pt / jetPt

    smearfact = None
    if isinstance(arr, awkward.array.base.AwkwardArray):
        wrap, arrays = awkward.util.unwrap_jagged(jetPt,
                                                  jetPt.JaggedArray,
                                                  (jetPt, min_jet_pt_corr))
        smearfact = np.where(doHybrid.content, detSmear.content, stochSmear.content)
        smearfact = np.where((smearfact * arrays[0]) < arrays[1], arrays[1], smearfact)
        smearfact = wrap(smearfact)
    elif isinstance(arr, awkward1.highlevel.Array):
        smearfact = awkward1.where(doHybrid, detSmear, stochSmear)
        smearfact = awkward1.where((smearfact * jetPt) < min_jet_pt,
                                   min_jet_pt_corr,
                                   smearfact)

        def getfunction(layout, depth):
            if (isinstance(layout, awkward1.layout.NumpyArray)
                or not isinstance(layout, (awkward1.layout.Content,
                                           awkward1.partition.PartitionedArray)
               )):
                return lambda: awkward1.layout.NumpyArray(smearfact)
            return None
        smearfact = awkward1._util.recursively_apply(
            awkward1.operations.convert.to_layout(jetPt),
            getfunction)
        smearfact = awkward1._util.wrap(smearfact, awkward1._util.behaviorof(jetPt))
    else:
        raise Exception('\'arr\' must be an awkward array of some kind!')

    return smearfact


class CorrectedJetsFactory(object):

    def __init__(self, name_map, jec_stack):
        # from PhysicsTools/PatUtils/interface/SmearedJetProducerT.h#L283
        self.forceStochastic = False

        if 'ptRaw' not in name_map or name_map['ptRaw'] is None:
            warnings.warn('There is no name mapping for ptRaw,'
                          ' CorrectedJets will assume that <object>.pt is raw pt!')
            name_map['ptRaw'] = name_map['JetPt'] + '_raw'
        self.treat_pt_as_raw = 'ptRaw' not in name_map

        if 'massRaw' not in name_map or name_map['massRaw'] is None:
            warnings.warn('There is no name mapping for massRaw,'
                          ' CorrectedJets will assume that <object>.mass is raw pt!')
            name_map['ptRaw'] = name_map['JetMass'] + '_raw'

        total_signature = set()
        for part in _stack_parts:
            attr = getattr(jec_stack, part)
            if part is not None:
                total_signature.update(attr.signature)

        missing = total_signature - set(name_map.keys())
        if len(missing) > 0:
            raise Exception(f'Missing mapping of {missing} in name_map!' +
                            ' Cannot evaluate jet corrections!' +
                            ' Please supply mappings for these variables!')

        if 'ptGenJet' not in name_map:
            warnings.warn('Input JaggedCandidateArray must have "ptGenJet" in order to apply hybrid JER smearing method. Stochastic smearing will be applied.')
            self.forceStochastic = True

        self.real_sig = [v for k, v in name_map.items()]
        self.name_map = name_map
        self.jec_stack = jec_stack

    def build(self, jets, lazy_cache=None):
        fields = None
        out = None
        wrap = None
        VirtualType = None
        isak1 = False
        if isinstance(jets, awkward.array.base.AwkwardArray):
            fields = jets.columns
            if len(fields) == 0:
                raise Exception('Detected awkward0: \'jets\' must have attributes specified in jets.columns!')
            wrap, out = awkward.util.unwrap_jagged(jets.copy())
            assert(len(out) == 1)
            out = out[0]
            VirtualType = awkward.VirtualArray
        elif isinstance(jets, awkward1.highlevel.Array):
            isak1 = True
            fields = awkward1.keys(jets)
            if len(fields) == 0:
                raise Exception('Detected awkward1: \'jets\' must have attributes specified by keys!')
            out = awkward1.flatten(jets)
            wrap = partial(awkward1_rewrap, like_what=jets, gfunc=rewrap_recordarray)
            VirtualType = awkward1.virtual
        else:
            raise Exception('\'jets\' must be an awkward array of some kind!')

        if len(fields) == 0:
            raise Exception('Empty record, please pass a jet object with at least {self.real_sig} defined!')

        # take care of nominal JEC (no JER if available)
        out[self.name_map['JetPt'] + '_orig'] = out[self.name_map['JetPt']]
        out[self.name_map['JetMass'] + '_orig'] = out[self.name_map['JetMass']]
        if self.treat_pt_as_raw:
            out[self.name_map['ptRaw']] = out[self.name_map['JetPt']]
            out[self.name_map['massRaw']] = out[self.name_map['JetMass']]

        jec_name_map = {}
        jec_name_map.update(self.name_map)
        jec_name_map['JetPt'] = jec_name_map['ptRaw']
        jec_name_map['JetMass'] = jec_name_map['ptRaw']
        jec_args = {k: out[jec_name_map[k]] for k in self.jec_stack.jec.signature}
        out['jet_energy_correction'] = self.jec_stack.jec.getCorrection(**jec_args, lazy_cache=lazy_cache)

        # finally the lazy binding to the JEC
        def jec_var_corr(arr, varName):
            return arr['jet_energy_correction'] * arr[varName]

        init_pt = partial(VirtualType, jec_var_corr, args=(out, self.name_map['ptRaw']), cache=lazy_cache)
        init_mass = partial(VirtualType, jec_var_corr, args=(out, self.name_map['massRaw']), cache=lazy_cache)
        out[self.name_map['JetPt']] = init_pt(length=len(out)) if isak1 else init_pt()
        out[self.name_map['JetMass']] = init_mass(length=len(out)) if isak1 else init_mass()

        out[self.name_map['JetPt'] + '_jec'] = init_pt(length=len(out)) if isak1 else init_pt()
        out[self.name_map['JetMass'] + '_jec'] = init_mass(length=len(out)) if isak1 else init_mass()

        # in jer we need to have a stash for the intermediate JEC products
        has_jer = False
        out_orig = copy(out)
        systs = {}
        if self.jec_stack.jer is not None and self.jec_stack.jersf is not None:
            has_jer = True
            jer_name_map = {}
            jer_name_map.update(self.name_map)
            jer_name_map['JetPt'] = jer_name_map['JetPt'] + '_jec'
            jer_name_map['JetMass'] = jer_name_map['JetMass'] + '_jec'

            jerargs = {k: out[jer_name_map[k]] for k in self.jec_stack.jer.signature}
            out['jet_energy_resolution'] = self.jec_stack.jer.getResolution(**jerargs, lazy_cache=lazy_cache)

            jersfargs = {k: out[jer_name_map[k]] for k in self.jec_stack.jersf.signature}
            out['jet_energy_resolution_scale_factor'] = self.jec_stack.jersf.getScaleFactor(**jersfargs, lazy_cache=lazy_cache)

            init_rand_gauss = partial(VirtualType, args=(out, jer_name_map['JetPt']), cache=lazy_cache)
            out['jet_resolution_rand_gauss'] = init_rand_gauss(rand_gauss_ak1, length=len(out)) if isak1 else init_rand_gauss(rand_gauss_ak0)

            def jer_smeared_corr(arr, variation, forceStochastic, ptGenJet, ptJet, etaJet):
                return jer_smear(arr, variation, forceStochastic, ptGenJet, ptJet, etaJet)

            init_jerc = partial(VirtualType,
                                jer_smeared_corr,
                                args=(out, 0, self.forceStochastic,
                                      jer_name_map['ptGenJet'],
                                      jer_name_map['JetPt'],
                                      jer_name_map['JetEta']),
                                cache=lazy_cache
                                )
            out['jet_energy_resolution_correction'] = init_jerc(length=len(out)) if isak1 else init_jerc()

            def jer_smeared_val(arr, varName):
                return arr['jet_energy_resolution_correction'] * arr[varName]

            init_pt_jer = partial(VirtualType, jer_smeared_val, args=(out, jer_name_map['JetPt']), cache=lazy_cache)
            init_mass_jer = partial(VirtualType, jer_smeared_val, args=(out, jer_name_map['JetPt']), cache=lazy_cache)
            out[self.name_map['JetPt']] = init_pt_jer(length=len(out)) if isak1 else init_pt_jer()
            out[self.name_map['JetMass']] = init_mass_jer(length=len(out)) if isak1 else init_mass_jer()

            out[self.name_map['JetPt'] + '_jer'] = init_pt_jer(length=len(out)) if isak1 else init_pt_jer()
            out[self.name_map['JetMass'] + '_jer'] = init_mass_jer(length=len(out)) if isak1 else init_mass_jer()

            out_orig = copy(out)

            # JER systematics
            jerc_up = partial(VirtualType,
                              jer_smeared_corr,
                              args=(out, 1, self.forceStochastic,
                                    jer_name_map['ptGenJet'],
                                    jer_name_map['JetPt'],
                                    jer_name_map['JetEta']),
                              cache=lazy_cache
                              )
            up = copy(out)
            up['jet_energy_resolution_correction'] = jerc_up(length=len(up)) if isak1 else jerc_up()
            init_pt_jer = partial(VirtualType, jer_smeared_val, args=(up, jer_name_map['JetPt']), cache=lazy_cache)
            init_mass_jer = partial(VirtualType, jer_smeared_val, args=(up, jer_name_map['JetPt']), cache=lazy_cache)
            up[self.name_map['JetPt']] = init_pt_jer(length=len(up)) if isak1 else init_pt_jer()
            up[self.name_map['JetMass']] = init_mass_jer(length=len(up)) if isak1 else init_mass_jer()

            jerc_down = partial(VirtualType,
                                jer_smeared_corr,
                                args=(out, 2, self.forceStochastic,
                                      jer_name_map['ptGenJet'],
                                      jer_name_map['JetPt'],
                                      jer_name_map['JetEta']),
                                cache=lazy_cache
                                )
            down = copy(out)
            down['jet_energy_resolution_correction'] = jerc_down(length=len(down)) if isak1 else jerc_down()
            init_pt_jer = partial(VirtualType, jer_smeared_val, args=(down, jer_name_map['JetPt']), cache=lazy_cache)
            init_mass_jer = partial(VirtualType, jer_smeared_val, args=(down, jer_name_map['JetPt']), cache=lazy_cache)
            down[self.name_map['JetPt']] = init_pt_jer(length=len(down)) if isak1 else init_pt_jer()
            down[self.name_map['JetMass']] = init_mass_jer(length=len(down)) if isak1 else init_mass_jer()
            systs['JER'] = awkward1.zip({'up': up, 'down': down}, depth_limit=1, with_name='JetSystematic')

        if self.jec_stack.junc:
            juncargs = {}
            juncargs.update(self.name_map)
            if has_jer:
                juncargs['JetPt'] = juncargs['JetPt'] + '_jer'
            else:
                juncargs['JetPt'] = juncargs['JetPt'] + '_jec'
            juncargs = {k: out[juncargs[k]] for k in self.jec_stack.junc.signature}
            juncs = self.jec_stack.junc.getUncertainty(**juncargs)
            for name, func in juncs:
                out[f'jet_energy_uncertainty_{name}'] = func

                def junc_smeared_val(arr, up_down, varName):
                    return arr[f'jet_energy_uncertainty_{name}'][:, up_down] * arr[varName]

                up = copy(out_orig)
                up[self.name_map['JetPt']] = VirtualType(junc_smeared_val,
                                                         args=(out, 0,
                                                               juncargs['JetPt'],
                                                               ),
                                                         length=len(out),
                                                         cache=lazy_cache)
                up[self.name_map['JetMass']] = VirtualType(junc_smeared_val,
                                                           args=(out, 0,
                                                                 juncargs['JetPt'],
                                                                 ),
                                                           length=len(out),
                                                           cache=lazy_cache)

                down = copy(out_orig)
                down[self.name_map['JetPt']] = VirtualType(junc_smeared_val,
                                                           args=(out, 0,
                                                                 juncargs['JetPt'],
                                                                 ),
                                                           length=len(out),
                                                           cache=lazy_cache)
                down[self.name_map['JetMass']] = VirtualType(junc_smeared_val,
                                                             args=(out, 0,
                                                                   juncargs['JetPt'],
                                                                   ),
                                                             length=len(out),
                                                             cache=lazy_cache)
                systs[f'JES_{name}'] = awkward1.zip({'up': up, 'down': down}, depth_limit=1, with_name='JetSystematic')

        outdict = {key: out_orig[key] for key in awkward1.keys(out_orig)}
        outdict.update(systs)

        out = awkward1.zip(outdict, depth_limit=1, with_name='Jet')

        return wrap(out)

import glob
import json
import logging
import os
import sys
from typing import Union, List, Dict, Tuple
from collections import OrderedDict

import numpy as np

from smac.utils.util_funcs import get_types
from smac.tae.execute_ta_run import StatusType
from smac.epm.rfr_imputator import RFRImputator
from smac.epm.rf_with_instances import RandomForestWithInstances

from pimp.configspace import CategoricalHyperparameter, Configuration, \
    FloatHyperparameter, IntegerHyperparameter, impute_inactive_values
from pimp.epm.unlogged_rf_with_instances import UnloggedRandomForestWithInstances
from pimp.evaluator.ablation import Ablation
from pimp.evaluator.fanova import fANOVA
from pimp.evaluator.incumbent_neighborhood import IncNeighbor
from pimp.evaluator.forward_selection import ForwardSelector, AbstractEvaluator
from pimp.evaluator.influence_models import InfluenceModel
from pimp.utils import RunHistory, RunHistory2EPM4Cost, RunHistory2EPM4LogCost, Scenario, average_cost

__author__ = "Andre Biedenkapp"
__copyright__ = "Copyright 2016, ML4AAD"
__license__ = "3-clause BSD"
__maintainer__ = "Andre Biedenkapp"
__email__ = "biedenka@cs.uni-freiburg.de"


class Importance(object):
    def __init__(self, scenario_file: Union[None, str] = None, scenario: Union[None, Scenario] = None,
                 runhistory_file: Union[str, None] = None, runhistory: Union[None, RunHistory] = None,
                 traj_file: Union[None, List[str]] = None, incumbent: Union[None, Configuration] = None,
                 seed: int = 12345, parameters_to_evaluate: int = -1, margin: Union[None, float] = None,
                 save_folder: str = 'PIMP', impute_censored: bool = False, max_sample_size: int = -1):
        """
        Importance Object. Handles the construction of the data and training of the model. Easy interface to the
        different evaluators.
        :param scenario_file: File to load the scenario from, if scenario is None.
        :param scenario: Scenario Object to use if scenario_file is None
        :param runhistory_file: File to load the runhistory from if runhistory is None.
        :param runhistory: Runhistory Object to use if runhistory_file is None.
        :param traj_file: File to load the trajectory from. If this is None but runhistory_file was specified,
               the trajectory will be read from the same directory as the runhistory.
               If both are None and incumbent is set, the incumbent configuration object will be used instead.
        :param incumbent: Configuration Object to use if no other means of loading the trajectory are given.
        :param seed: Seed used for the numpy random generator.
        :param parameters_to_evaluate: int that specifies how many parameters have to be evaluated.
               If set to -1 all parameters will be evaluated.
        :param margin: float used in conjunction with influence models. Is the minimal improvement to accept a
                       parameter as important.
        :param save_folder: Folder name to save the output to
        :param impute_censored: boolean that specifies if censored data should be imputed. If not, censored data are
               ignored.
        """
        self.logger = logging.getLogger("Importance")
        self.rng = np.random.RandomState(seed)
        self._parameters_to_evaluate = parameters_to_evaluate
        self._evaluator = None
        self.margin = margin
        self.threshold = None
        self.seed = seed
        self.impute = impute_censored

        self._setup_scenario(scenario, scenario_file, save_folder)
        self._load_runhist(runhistory, runhistory_file)
        self._setup_model()
        self._load_incumbent(traj_file, runhistory_file, incumbent)
        if 0 < max_sample_size < len(self.X):
            idx = list(range(len(self.X)))
            np.random.shuffle(idx)
            idx = idx[:max_sample_size]
            self.X = self.X[idx]
            self.y = self.y[idx]
            self.logger.info('Remaining %d datapoints' % len(self.X))
            self.model.train(self.X, self.y)


    def _setup_scenario(self, scenario: Union[None, Scenario], scenario_file: Union[None, str], save_folder: str) -> \
            None:
        """
        Setup for the scenario
        Helper method to have the init method less cluttered.
        For parameter specifications, see __init__
        """
        if scenario is not None:
            self.scenario = scenario
        elif scenario_file is not None:
            self.logger.info('Reading Scenario file and files specified in the scenario')
            self.scenario = Scenario(scenario=scenario_file, cmd_args={'output_dir': ""}, run_id=1)
            self.scenario.output_dir = save_folder
            self.scenario.out_writer.write_scenario_file(self.scenario)
        else:
            raise Exception('Either a scenario has to be given or a file to load it from! Both were set to None!')

    def _load_incumbent(self, traj_file, runhistory_file, incumbent, predict_best=True) -> None:
        """
        Handles the loading of the incumbent according to the given parameters.
        Helper method to have the init method less cluttered.
        For parameter specifications, see __init__
        """
        self.incumbent = (None, None)
        if traj_file is not None:
            self.incumbent = self._read_traj_file(traj_file)[0]
            self.logger.debug('Incumbent %s' % str(self.incumbent))
        elif traj_file is None and runhistory_file is not None:
            traj_files = os.path.join(os.path.dirname(runhistory_file), 'traj_aclib2.json')
            traj_files = sorted(glob.glob(traj_files, recursive=True))
            incumbents = []
            for traj_ in traj_files:
                self.logger.info('Reading traj_file: %s' % traj_)
                incumbents.append(self._read_traj_file(traj_))
                incumbents[-1].extend(self._model.predict_marginalized_over_instances(
                    np.array([impute_inactive_values(incumbents[-1][0]).get_array()])))
                self.logger.debug(incumbents[-1])
            sort_idx = 2 if predict_best else 1
            incumbents = sorted(incumbents, key=lambda x: x[sort_idx])
            self.incumbent = incumbents[0][0]
            self.logger.info('Incumbent %s' % str(self.incumbent))
        elif incumbent is not None:
            self.incumbent = incumbent
        else:
            raise Exception('No method specified to load an incumbent. Either give the incumbent directly or specify '
                            'a file to load it from!')

    def _setup_model(self) -> None:
        """
        Sets up all the necessary parameters used for the model.
        Helper method to have the init method less cluttered.
        For parameter specifications, see __init__
        """
        self.logger.info('Converting Data and constructing Model')
        self.X = None
        self.y = None
        self.types = None
        self.bounds = None
        self._model = None
        self.logged_y = False
        self._convert_data()

    def _load_runhist(self, runhistory, runhistory_file) -> None:
        """
        Handels loading of the runhistory/runhistories.
        Helper method to have the init method less cluttered.
        For parameter specifications, see __init__
        """
        self.logger.debug(runhistory_file)
        self.logger.debug(runhistory)
        if runhistory is not None:
            self.runhistory = runhistory
        elif runhistory_file is not None:
            self.logger.info('Reading Runhistory')
            self.runhistory = RunHistory(aggregate_func=average_cost)

            globed_files = glob.glob(runhistory_file)
            self.logger.info('#RunHistories found: %d' % len(globed_files))
            if not globed_files:
                self.logger.error('No runhistory files found!')
                sys.exit(1)
            self.runhistory.load_json(globed_files[0], self.scenario.cs)
            for rh_file in globed_files[1:]:
                self.runhistory.update_from_json(rh_file, self.scenario.cs)
        else:
            raise Exception('Either a runhistory or files to load them from have to be specified! Both were set to '
                            'None!')
        self.logger.info('Combined number of Runhistory data points: %d' % len(self.runhistory.data))
        self.logger.info('Number of Configurations: %d' % (len(self.runhistory.get_all_configs())))

    def _read_traj_file(self, fn):
        """
        Simple method to read in a trajectory file in the json format / aclib2 format
        :param fn:
            file name
        :return:
            tuple of (incumbent [Configuration], incumbent_cost [float])
        """
        if not (os.path.exists(fn) and os.path.isfile(fn)):  # File existence check
            raise FileNotFoundError('File %s not found!' % fn)
        with open(fn, 'r') as fh:
            for line in fh.readlines():
                pass
        line = line.strip()
        incumbent_dict = json.loads(line)
        inc_dict = {}
        for key_val in incumbent_dict['incumbent']:  # convert string to Configuration
            key, val = key_val.replace("'", '').split('=')
            if isinstance(self.scenario.cs.get_hyperparameter(key), (CategoricalHyperparameter)):
                inc_dict[key] = val
            elif isinstance(self.scenario.cs.get_hyperparameter(key), (FloatHyperparameter)):
                inc_dict[key] = float(val)
            elif isinstance(self.scenario.cs.get_hyperparameter(key), (IntegerHyperparameter)):
                inc_dict[key] = int(val)
        incumbent = Configuration(self.scenario.cs, inc_dict)
        incumbent_cost = incumbent_dict['cost']
        return [incumbent, incumbent_cost]

    @property
    def model(self):
        return self._model

    @model.setter
    def model(self, model_short_name='urfi'):
        self.types, self.bounds = get_types(self.scenario.cs, self.scenario.feature_array)
        if model_short_name not in ['urfi', 'rfi']:
            raise ValueError('Specified model %s does not exist or not supported!' % model_short_name)
        elif model_short_name == 'rfi':
            self._model = RandomForestWithInstances(self.types, self.bounds,
                                                    instance_features=self.scenario.feature_array,
                                                    seed=self.rng.randint(99999))
        elif model_short_name == 'urfi':
            self._model = UnloggedRandomForestWithInstances(self.types, self.bounds,
                                                            self.scenario.feature_array,
                                                            seed=self.rng.randint(99999),
                                                            cutoff=self.cutoff, threshold=self.threshold)
        self._model.rf_opts.compute_oob_error = True

    @property
    def evaluator(self) -> AbstractEvaluator:
        """
        Getter of the evaluator property. Returns the set evaluation method.
        :return: AbstractEvaluator
        """
        return self._evaluator

    @evaluator.setter
    def evaluator(self, evaluation_method: str) -> None:
        """
        Setter of the evaluator property. The wanted evaluation method can be specified as string and the rest is
        handled automatically here
        :param evaluation_method: Name of the evaluation method to use
        :return: None
        """
        if self._model is None:
            self._setup_model()
        self.logger.info('Setting up Evaluation Method')
        if evaluation_method not in ['ablation', 'fanova', 'forward-selection', 'influence-model',
                                     'incneighbor']:
            raise ValueError('Specified evaluation method %s does not exist!' % evaluation_method)
        if evaluation_method == 'ablation':
            if self.incumbent is None:
                raise ValueError('Incumbent is %s!\n \
                                 Incumbent has to be read from a trajectory file before ablation can be used!'
                                 % self.incumbent)
            self.logger.info('Using model %s' % str(self.model))
            self.logger.info('X shape %s' % str(self.model.X.shape))
            evaluator = Ablation(scenario=self.scenario,
                                 cs=self.scenario.cs,
                                 model=self._model,
                                 to_evaluate=self._parameters_to_evaluate,
                                 incumbent=self.incumbent,
                                 logy=self.logged_y, rng=self.rng)
        elif evaluation_method == 'influence-model':
            self.logger.info('Using model %s' % str(self.model))
            self.logger.info('X shape %s' % str(self.model.X.shape))
            evaluator = InfluenceModel(scenario=self.scenario,
                                       cs=self.scenario.cs,
                                       model=self._model,
                                       to_evaluate=self._parameters_to_evaluate,
                                       margin=self.margin,
                                       threshold=self.threshold, rng=self.rng)
        elif evaluation_method == 'fanova':
            self.logger.info('Using model %s' % str(self.model))
            self.logger.info('X shape %s' % str(self.model.X.shape))
            evaluator = fANOVA(scenario=self.scenario,
                               cs=self.scenario.cs,
                               model=self._model,
                               to_evaluate=self._parameters_to_evaluate,
                               runhist=self.runhistory, rng=self.rng)
        elif evaluation_method == 'incneighbor':
            if self.incumbent is None:
                raise ValueError('Incumbent is %s!\n \
                                 Incumbent has to be read from a trajectory file before ablation can be used!'
                                 % self.incumbent)
            self.logger.info('Using model %s' % str(self.model))
            self.logger.info('X shape %s' % str(self.model.X.shape))
            evaluator = IncNeighbor(scenario=self.scenario,
                                    cs=self.scenario.cs,
                                    model=self._model,
                                    to_evaluate=self._parameters_to_evaluate,
                                    incumbent=self.incumbent,
                                    logy=self.logged_y, rng=self.rng)
        else:
            self.logger.info('Using model %s' % str(self.model))
            evaluator = ForwardSelector(scenario=self.scenario,
                                        cs=self.scenario.cs,
                                        model=self._model,
                                        to_evaluate=self._parameters_to_evaluate, rng=self.rng)
        self._evaluator = evaluator

    def _convert_data(self) -> None:  # From Marius
        '''
            converts data from runhistory into EPM format

            Parameters
            ----------
            scenario: Scenario
                smac.scenario.scenario.Scenario Object
            runhistory: RunHistory
                smac.runhistory.runhistory.RunHistory Object with all necessary data

            Returns
            -------
            np.array
                X matrix with configuartion x features for all observed samples
            np.array
                y matrix with all observations
            np.array
                types of X cols -- necessary to train our RF implementation
        '''

        params = self.scenario.cs.get_hyperparameters()
        num_params = len(params)

        if self.scenario.run_obj == "runtime":
            self.cutoff = self.scenario.cutoff
            self.threshold = self.scenario.cutoff * self.scenario.par_factor
            self.model = 'urfi'
            self.logged_y = True
            # if we log the performance data,
            # the RFRImputator will already get
            # log transform data from the runhistory
            cutoff = np.log10(self.scenario.cutoff)
            threshold = np.log10(self.scenario.cutoff *
                                 self.scenario.par_factor)
            model = RandomForestWithInstances(self.types, self.bounds,
                                              instance_features=self.scenario.feature_array,
                                              seed=self.rng.randint(99999), do_bootstrapping=True,
                                              num_trees=80, n_points_per_tree=50000)

            imputor = RFRImputator(rng=self.rng,
                                   cutoff=cutoff,
                                   threshold=threshold,
                                   model=model,
                                   change_threshold=0.01,
                                   max_iter=10)
            rh2EPM = RunHistory2EPM4LogCost(scenario=self.scenario,
                                            num_params=num_params,
                                            success_states=[
                                                StatusType.SUCCESS, ],
                                            impute_censored_data=self.impute,
                                            impute_state=[
                                                StatusType.TIMEOUT, StatusType.CAPPED],
                                            imputor=imputor)
        else:
            self.model = 'rfi'
            rh2EPM = RunHistory2EPM4Cost(scenario=self.scenario,
                                         num_params=num_params,
                                         success_states=None,
                                         impute_censored_data=self.impute,
                                         impute_state=None)
        self.logger.info('Using model %s' % str(self.model))
        X, Y = rh2EPM.transform(self.runhistory)

        self.X = X
        self.y = Y
        self.logger.info('Size of training X: %s' % str(self.X.shape))
        self.logger.info('Size of training y: %s' % str(self.y.shape))
        self.logger.info('Data was %s imputed' % ('not' if not self.impute else ''))
        if not self.impute:
            self.logger.info('Thus the size of X might be smaller than the datapoints in the RunHistory')
        self.model.train(X, Y)

    def evaluate_scenario(self, evaluation_method='all',
                          sort_by: int = 0) -> Union[Tuple[Dict[str, Dict[str, float]], List[AbstractEvaluator]],
                                                     Dict[str, Dict[str, float]]]:
        """
        Evaluate the given scenario
        :param evaluation_method: name of the method to use
        :param sort_by: int, determines the order (only used if evaluation_method == all)
            0 => Ablation, fANOVA, Forward Selection
            1 => Ablation, Forward Selection, fANOVA
            2 => fANOVA, Forward Selection, Ablation
            3 => fANOVA, Ablation, Forward Selection
            4 => Forward Selection, Ablation, fANOVA
            5 => Forward Selection, fANOVA, Ablation
        :return: if evaluation all: Tupel of dictionary[evaluation_method] -> importance values, List ov evaluator
                                    names, ordered according to sort_by
                 else:
                      dict[evalution_method] -> importance values
        """
        # influence-model currently not supported
        # influence-model currently not supported
        methods = ['ablation', 'fanova', 'forward-selection', 'incneighbor']
        if sort_by == 1:
            methods = ['ablation', 'forward-selection', 'fanova', 'incneighbor']
        elif sort_by == 2:
            methods = ['fanova', 'forward-selection', 'ablation', 'incneighbor']
        elif sort_by == 3:
            methods = ['fanova', 'ablation', 'forward-selection', 'incneighbor']
        elif sort_by == 4:
            methods = ['forward-selection', 'ablation', 'fanova', 'incneighbor']
        elif sort_by == 5:
            methods = ['forward-selection', 'fanova', 'ablation', 'incneighbor']
        if evaluation_method == 'all':
            evaluators = []
            dict_ = {}
            for method in methods:
                self.logger.info('Running %s' % method)
                self.evaluator = method
                dict_[method] = self.evaluator.run()
                evaluators.append(self.evaluator)
            return dict_, evaluators
        else:
            self.evaluator = evaluation_method
            self.logger.info('Running evaluation method %s' % self.evaluator.name)
            return {evaluation_method: self.evaluator.run()}

    def plot_results(self, name: Union[List[str], str, None] = None, evaluators: Union[List[AbstractEvaluator],
                                                                                       None] = None,
                     show: bool = True):
        """
        Method to handle the plotting in case of plots for multiple evaluation methods or only one
        :param name: name(s) to save the plot(s) with
        :param evaluators: list of ealuators to generate the plots for
        :param show: boolean. Specifies if the results have to additionally be shown and not just saved!
        :return:
        """
        if evaluators:
            for eval, name_ in zip(evaluators, name):
                eval.plot_result(name_, show)
        else:
            self.evaluator.plot_result(name, show)

    def table_for_comparison(self, evaluators: List[AbstractEvaluator], name: Union[None, str] = None, style='cmd'):
        """
        Small Method that creates an output table for comparison either printed in a readable format for the command
        line or in latex style
        :param evaluators: All evaluators to put into the table
        :param name: Name for the save file name
        :param style: (cmd|latex) str to determine which format to use
        :return: None
        """
        if name:
            f = open(name, 'w')
        else:
            f = sys.stderr
        header = ['{:>{width}s}' for _ in range(len(evaluators) + 1)]
        line = '-' if style == 'cmd' else '\hline'
        join_ = ' | ' if style == 'cmd' else ' & '
        body = OrderedDict()
        _max_len_p = 1
        _max_len_h = 1
        for idx, e in enumerate(evaluators):
            for p in e.evaluated_parameter_importance:
                if p not in ['-source-', '-target-']:
                    if p not in body:
                        body[p] = ['-' for _ in range(len(evaluators))]
                        body[p][idx] = e.evaluated_parameter_importance[p]
                        _max_len_p = max(_max_len_p, len(p))
                    else:
                        body[p][idx] = e.evaluated_parameter_importance[p]
                    if e.name in ['Ablation', 'fANOVA']:
                        if body[p][idx] != '-':
                            body[p][idx] *= 100
                        _max_len_p = max(_max_len_p, len(p))

            header[idx + 1] = e.name
            _max_len_h = max(_max_len_h, len(e.name))
        header[0] = header[0].format(' ', width=_max_len_p)
        header[1:] = list(map(lambda x: '{:^{width}s}'.format(x, width=_max_len_h), header[1:]))
        header = join_.join(header)
        if style == 'latex':
            print('\\begin{table}', file=f)
            print('\\begin{tabular}{r%s}' % ('|r' * len(evaluators)), file=f)
            print('\\toprule', file=f)
        print(header, end='\n' if style == 'cmd' else '\\\\\n', file=f)
        if style == 'cmd':
            print(line * len(header), file=f)
        else:
            print(line, file=f)
        for p in body:
            if style == 'cmd':
                b = ['{:>{width}s}'.format(p, width=_max_len_p)]
            else:
                b = ['{:<{width}s}'.format(p, width=_max_len_p)]
            for x in body[p]:
                try:
                    if style == 'latex':
                        b.append('${:> {width}.3f}$'.format(x, width=_max_len_h - 2))
                    else:
                        b.append('{:> {width}.3f}'.format(x, width=_max_len_h))
                except ValueError:
                    b.append('{:>{width}s}'.format(x, width=_max_len_h))
            print(join_.join(b), end='\n' if style == 'cmd' else '\\\\\n', file=f)
        cap = 'Parameter Importance values, obtained using the PIMP package. Ablation values are percentages ' \
              'of improvement a single parameter change obtained between the default and an' \
              ' incumbent configuration.\n' \
              'fANOVA values are percentages that show how much variance across the whole ConfigSpace can be ' \
              'explained by that parameter.\n' \
              'Forward Selection values are RMSE values obtained using only a subset of parameters for prediction.\n' \
              'fANOVA and Forward Selection try to estimate the importances across the whole parameter space, while ' \
              'ablation tries to estimate them between two given configurations.'
        if self._parameters_to_evaluate > 0:
            cap += """\nOnly the top %d parameters of each method are listed.
                    "-" represent that this parameter was not evaluated
                     using the given method but with another.
                    """ % self._parameters_to_evaluate
        if style == 'latex':
            print('\\bottomrule', file=f)
            print('\end{tabular}', file=f)
            print('\\caption{%s}' % cap,
                  file=f)
            print('\\label{tab:pimp}', file=f)
            print('\end{table}', file=f)
        else:
            print('', file=f)
            print(cap)
        if name:
            f.close()

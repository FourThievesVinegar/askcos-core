import os
import time
import makeit.global_config as gc
from makeit.utilities.io import arg_parser, name_parser, files
import rdkit.Chem as Chem
from makeit.utilities.io.logging import MyLogger
from makeit.retrosynthetic.tree_builder import TreeBuilder
from askcos_site.askcos_celery.treebuilder.tb_coordinator import get_buyable_paths
from askcos_site.askcos_celery.treeevaluator.tree_evaluation_coordinator import evaluate_trees
from makeit.synthetic.evaluation.tree_evaluator import TreeEvaluator
import sys
makeit_loc = 'makeit'


class MAKEIT:
    '''
    Main application for running the make-it program. 
    Proposes potential synthetic routes to a desired target compound in two steps:
     - Building a retro synthetic tree and extracting buyable routes
     - Evaluation the likelihood of succes of each of the reactions in the found buyable routes
     - Returns all (or some) of the likely synthetic routes
    '''

    def __init__(self, TARGET, expansion_time, max_depth, max_branching, max_trees, retro_mincount, retro_mincount_chiral,
                 synth_mincount, rank_threshold_inclusion, prob_threshold_inclusion, max_total_contexts, template_count,
                 max_ppg, output_dir, chiral, nproc, celery, context_recommender, forward_scoring_method,
                 tree_scoring_method, context_prioritization, template_prioritization, precursor_prioritization, 
                 parallel_tree, precursor_score_mode, max_cum_template_prob):

        self.TARGET = TARGET
        self.expansion_time = expansion_time
        self.max_depth = max_depth
        self.max_branching = max_branching
        self.max_trees = max_trees
        self.context_recommender = context_recommender
        self.forward_scoring_method = forward_scoring_method
        self.tree_scoring_method = tree_scoring_method
        self.context_prioritization = context_prioritization
        self.template_prioritization = template_prioritization
        self.precursor_prioritization = precursor_prioritization
        self.retro_mincount = retro_mincount
        self.retro_mincount_chiral = retro_mincount_chiral
        self.synth_mincount = synth_mincount
        self.rank_threshold_inclusion = rank_threshold_inclusion
        self.prob_threshold_inclusion = prob_threshold_inclusion
        self.max_total_contexts = max_total_contexts
        self.precursor_score_mode = precursor_score_mode
        self.max_ppg = max_ppg
        self.mol = name_parser.name_to_molecule(TARGET)
        self.max_cum_template_prob = max_cum_template_prob
        self.smiles = Chem.MolToSmiles(self.mol)
        self.ROOT = files.make_directory(output_dir)
        self.case_dir = files.make_directory(
            '{}/{}'.format(self.ROOT, self.TARGET))
        self.nproc = nproc
        self.celery = celery
        self.chiral = chiral
        self.known_bad_reactions = []
        self.template_count = template_count
        self.parallel_tree = parallel_tree
        
    def construct_buyable_trees(self):

        if self.celery:  # Call celery worker
            working = time.time()
            res = get_buyable_paths.apply_async(args=(self.smiles, self.template_prioritization, self.precursor_prioritization),
                                                kwargs={'mincount': self.retro_mincount, 'max_branching': self.max_branching,
                                                        'max_depth': self.max_depth, 'max_ppg': self.max_ppg, 'max_time': self.expansion_time,
                                                        'max_trees': self.max_trees, 'known_bad_reactions': self.known_bad_reactions,
                                                        'chiral': self.chiral, 'template_count':self.template_count,
                                                        'precursor_score_mode':self.precursor_score_mode,
                                                        'max_cum_template_prob':self.max_cum_template_prob})

            while not res.ready():
                if int(time.time() - working) % 10 == 0:
                    MyLogger.print_and_log('Building trees...', makeit_loc)
                time.sleep(1)
            buyable_trees = res.get()
        else:  # Create tree builder object and run it
            treeBuilder = TreeBuilder(celery=self.celery, mincount=self.retro_mincount,
                                      mincount_chiral=self.retro_mincount_chiral, chiral=self.chiral)

            buyable_trees = treeBuilder.get_buyable_paths(self.smiles, template_prioritization=self.template_prioritization,
                                                          precursor_prioritization=self.precursor_prioritization, nproc=self.nproc,
                                                          max_depth=self.max_depth, max_branching=self.max_branching, max_ppg=self.max_ppg,
                                                          mincount=self.retro_mincount, chiral=self.chiral, max_trees=self.max_trees,
                                                          known_bad_reactions=self.known_bad_reactions, expansion_time=self.expansion_time,
                                                          template_count = self.template_count, precursor_score_mode=self.precursor_score_mode,
                                                          max_cum_template_prob = self.max_cum_template_prob)

        return buyable_trees

    def evaluate_synthesis_trees(self, trees):
        if self.celery:  # Call celery worker
            working = time.time()
            res = evaluate_trees.apply_async(args=(trees,), kwargs={'context_scoring_method': self.context_prioritization,
                                                                    'context_recommender': self.context_recommender,
                                                                    'forward_scoring_method': self.forward_scoring_method,
                                                                    'tree_scoring_method': self.tree_scoring_method,
                                                                    'rank_threshold': self.rank_threshold_inclusion,
                                                                    'prob_threshold': self.prob_threshold_inclusion,
                                                                    'mincount': self.synth_mincount,
                                                                    'batch_size': 500, 'n': self.max_total_contexts,
                                                                    'template_count':self.template_count,
                                                                    })
            while not res.ready():
                if int(time.time() - working) % 10 == 0:
                    MyLogger.print_and_log('Evaluating trees...', makeit_loc)
                time.sleep(1)
            evaluated_trees = res.get()
        else:  # Create a tree evaluation object and run it
            if self.forward_scoring_method == gc.templatebased:
                # nproc = number of parallel forward enumeration workers
                # nproc_t = number of trees to be evaluated in parallel.
                # Only use an nproc different from 1 if using the template base forward evaluation method. Otherwise
                # evaluation is fast enough to do without additional
                # parallelization
                if len(trees) > 2 or self.parallel_tree:
                    nproc_t = 2
                    nproc = max(1, self.nproc/2)
                else:
                    nproc_t = 1
                    nproc = self.nproc
            else:
                nproc_t = max(1, self.nproc)
                nproc = 1
            treeEvaluator = TreeEvaluator(
                celery=False, context_recommender=self.context_recommender)
            evaluated_trees = treeEvaluator.evaluate_trees(trees, context_recommender=self.context_recommender, context_scoring_method=self.context_prioritization,
                                                           forward_scoring_method=self.forward_scoring_method, tree_scoring_method=self.tree_scoring_method,
                                                           rank_threshold=self.rank_threshold_inclusion, prob_threshold=self.prob_threshold_inclusion,
                                                           mincount=self.synth_mincount, batch_size=500, n=self.max_total_contexts, nproc_t=nproc_t,
                                                           nproc=nproc, parallel=self.parallel_tree, template_count = self.template_count,
                                                           )
        plausible_trees = []
        print evaluated_trees
        for tree in evaluated_trees:
            if tree['plausible']:
                plausible_trees.append(tree)

        if plausible_trees:
            MyLogger.print_and_log(
                'Feasible synthesis route discovered!', makeit_loc)
        else:
            MyLogger.print_and_log(
                'No feasible routes from buyables have been discovered. Consider changing inclusion thesholds.', makeit_loc)

        return plausible_trees


def print_at_depth(chemical_node, depth=1):
    MyLogger.print_and_log('{}(${}/g) {}'.format(depth*4*' ',
                                                 chemical_node['ppg'], chemical_node['smiles']), makeit_loc)
    if chemical_node['children']:
        rxn = chemical_node['children'][0]
        MyLogger.print_and_log('{}smiles : {}'.format(
            (depth*4+4)*' ', rxn['smiles']), makeit_loc)
        MyLogger.print_and_log('{}num ex : {}'.format(
            (depth*4+4)*' ', rxn['num_examples']), makeit_loc)
        MyLogger.print_and_log('{}context: {}'.format(
            (depth*4+4)*' ', rxn['context']), makeit_loc)
        MyLogger.print_and_log('{}score  : {}'.format(
            (depth*4+4)*' ', rxn['forward_score']), makeit_loc)
        for child_node in rxn['children']:
            print_at_depth(child_node, depth=depth+1)


def find_synthesis():

    args = arg_parser.get_args()
    makeit = MAKEIT(args.TARGET, args.expansion_time, args.max_depth, args.max_branching,
                    args.max_trees, args.retro_mincount, args.retro_mincount_chiral, args.synth_mincount,
                    args.rank_threshold, args.prob_threshold, args.max_contexts, args.template_count, args.max_ppg,
                    args.output, args.chiral, args.nproc, args.celery, args.context_recommender,
                    args.forward_scoring, args.tree_scoring, args.context_prioritization,
                    args.template_prioritization, args.precursor_prioritization, args.parallel_tree,
                    args.precursor_score_mode, args.max_cum_template_prob)
    MyLogger.initialize_logFile(makeit.ROOT, makeit.case_dir)

    tree_status, trees = makeit.construct_buyable_trees()
    MyLogger.print_and_log(
        'MAKEIT generated {} buyable tree(s) that meet(s) all constraints.'.format(len(trees)), makeit_loc)
    feasible_trees = makeit.evaluate_synthesis_trees(trees)
    MyLogger.print_and_log('MAKEIT found {} tree(s) that are(is) likely to result in a successful synthesis.'.format(
        len(feasible_trees)), makeit_loc)

    for i, feasible_tree in enumerate(sorted(feasible_trees, key=lambda x: x['score'], reverse=True)):
        MyLogger.print_and_log('', makeit_loc)
        MyLogger.print_and_log('Feasible tree {}, plausible = {}, overall score = {}'.format(i+1,
                feasible_tree['plausible'], feasible_tree['score']), makeit_loc)
        print_at_depth(feasible_tree['tree'])

if __name__ == '__main__':
    find_synthesis()

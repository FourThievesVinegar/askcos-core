import itertools
import time

import networkx as nx
import numpy as np
from rdkit import Chem
from rdchiral.initialization import rdchiralReaction, rdchiralReactants

import makeit.global_config as gc
from makeit.retrosynthetic.transformer import RetroTransformer
from makeit.utilities.buyable.pricer import Pricer
from makeit.prioritization.templates.relevance import RelevanceTemplatePrioritizer


class MCTS:
    """Monte Carlo Tree Search"""

    def __init__(self, pricer=None, retro_transformer=None, use_db=False,
                 template_set='reaxys', template_prioritizer='reaxys',
                 precursor_prioritizer='relevanceheuristic', fast_filter='default'):

        self.tree = nx.DiGraph()  # directed graph

        self.target = None  # the target compound

        self.chemicals = []  # list of chemical smiles
        self.reactions = []  # list of reaction smiles

        self.pricer = pricer or self.load_pricer(use_db)
        self.retro_transformer = retro_transformer or self.load_retro_transformer(
            use_db=use_db,
            template_set=template_set,
            template_prioritizer=template_prioritizer,
            precursor_prioritizer=precursor_prioritizer,
            fast_filter=fast_filter,
        )

        self.template_max_count = 100
        self.template_max_cum_prob = 0.995

        self.fast_filter_threshold = 0.75

        self.max_branching = 10
        self.max_depth = 3
        self.exploration_weight = 1.0

        self.max_ppg = 10

        self.expansion_time = 20
        self.max_chemicals = None
        self.max_reactions = None

    @property
    def done(self):
        """
        Determine if we're done expanding the tree.
        """
        return (
            self.is_chemical_done(self.target)
            or (self.max_chemicals is not None and len(self.chemicals) >= self.max_chemicals)
            or (self.max_reactions is not None and len(self.reactions) >= self.max_reactions)
        )

    def to_branching(self):
        """
        Get branching representation of the tree.
        """
        branching = nx.dag_to_branching(self.tree)
        # Copy node attributes from original graph
        for node, data in branching.nodes(data=True):
            smiles = data['source']
            data.update(self.tree.nodes[smiles])
        return branching

    @staticmethod
    def load_pricer(use_db):
        """
        Loads pricer.
        """
        pricer = Pricer(use_db=use_db)
        pricer.load()
        return pricer

    @staticmethod
    def load_retro_transformer(use_db, template_set='reaxys', template_prioritizer='reaxys',
                               precursor_prioritizer='relevanceheuristic', fast_filter='default'):
        """
        Loads retro transformer model.
        """
        retro_transformer = RetroTransformer(
            use_db=use_db,
            template_set=template_set,
            template_prioritizer=template_prioritizer,
            precursor_prioritizer=precursor_prioritizer,
            fast_filter=fast_filter,
        )
        retro_transformer.load()
        return retro_transformer

    def build_tree(self, target):
        """
        Build retrosynthesis tree by iterative expansion of precursor nodes.
        """
        print('Initializing tree...')
        self._initialize(target)

        print('Starting tree expansion...')
        start_time = time.time()
        elapsed_time = time.time() - start_time

        while elapsed_time < self.expansion_time and not self.done:
            print('.', end='')
            self._rollout()
            elapsed_time = time.time() - start_time

        print('\nTree expansion complete.')
        self.print_stats()

    def print_stats(self):
        """
        Print tree statistics.
        """
        info = ''
        num_nodes = self.tree.number_of_nodes()
        info += "Number of nodes: {0:d}\n".format(num_nodes)
        info += "    Chemical nodes: {0:d}\n".format(len(self.chemicals))
        info += "    Reaction nodes: {0:d}\n".format(len(self.reactions))
        info += "Number of edges: {0:d}\n".format(self.tree.number_of_edges())
        if num_nodes > 0:
            info += "Average in degree: {0:.4f}\n".format(sum(d for _, d in self.tree.in_degree()) / num_nodes)
            info += "Average out degree: {0:.4f}".format(sum(d for _, d in self.tree.out_degree()) / num_nodes)
        print(info)

    def clear(self):
        """
        Clear tree and reset chemicals and reactions.
        """
        self.tree.clear()
        self.chemicals = []
        self.reactions = []

    def _initialize(self, target):
        """
        Initialize the tree by with the target chemical.
        """
        self.target = target
        self.create_chemical_node(self.target)
        self.tree.nodes[self.target]['terminal'] = False
        self.tree.nodes[self.target]['done'] = False
        self.tree.nodes[self.target]['visit_count'] += 1

    def _rollout(self):
        """
        Perform one iteration of tree expansion
        """
        chem_path, rxn_path, template = self._select()
        self._expand(chem_path, template)
        self._update(chem_path, rxn_path)

    def _expand(self, chem_path, template):
        """
        Expand the tree by applying chosen template to a chemical node.
        """
        leaf = chem_path[-1]
        explored = self.tree.nodes[leaf]['explored']
        if template not in explored:
            explored.append(template)
            precursors = self._get_precursors(leaf, template)
            self._process_precursors(leaf, template, precursors, chem_path)

    def _update(self, chem_path, rxn_path):
        """
        Update status and reward for nodes in this path.

        Reaction nodes are guaranteed to only have a single parent. Thus, the
        status of its parent chemical will always be updated appropriately in
        ``_update`` and will not change until the next time the chemical is
        in the selected path. Thus, the done state of the chemical can be saved.

        However, chemical nodes can have multiple parents (i.e. can be reached
        via multiple reactions), so a given update cycle may only pass through
        one of multiple parent reactions. Thus, the done state of a reaction
        must be determined dynamically and cannot be saved.
        """
        assert chem_path[0] == self.target, 'Chemical path should start at the root node.'

        # Iterate over the full path in reverse
        # On each iteration, rxn will be the parent reaction of chem
        # For the root (target) node, rxn will be None
        for i, chem, rxn in itertools.zip_longest(range(len(chem_path)-1, -1, -1), reversed(chem_path), reversed(rxn_path)):
            chem_data = self.tree.nodes[chem]
            chem_data['visit_count'] += 1
            chem_data['min_depth'] = min(chem_data['min_depth'], i) if chem_data['min_depth'] is not None else i
            self.is_chemical_done(chem, update=True)
            if rxn is not None:
                rxn_data = self.tree.nodes[rxn]
                rxn_data['visit_count'] += 1

    def is_chemical_done(self, smiles, update=False):
        """
        Determine if the specified chemical node should be expanded further.

        If ``update=True``, will reassess the done state of the node, update
        the ``done`` attribute, and return the new result.

        Otherwise, return the ``done`` node attribute.

        Chemical nodes are done when one of the following is true:
        - The node is terminal
        - The node has exceeded max_depth
        - The node as exceeded max_branching
        - The node does not have any templates to expand
        """
        if update:
            data = self.tree.nodes[smiles]
            done = False
            if data['terminal']:
                done = True
            elif len(data['templates']) == 0:
                done = True
            elif data['min_depth'] is not None and data['min_depth'] >= self.max_depth:
                done = True
            elif self.tree.out_degree(smiles) >= self.max_branching or len(data['explored']) == len(data['templates']):
                done = all(self.is_reaction_done(r) for r in self.tree.successors(smiles))
            data['done'] = done
            return done
        else:
            return self.tree.nodes[smiles]['done']

    def is_reaction_done(self, smiles):
        """
        Determine if the specified reaction node should be expanded further.

        Reaction nodes are done when all of its children chemicals are done.
        """
        return self.tree.out_degree(smiles) > 0 and all(self.is_chemical_done(c) for c in self.tree.successors(smiles))

    def _select(self):
        """
        Select next leaf node to be expanded.

        This starts at the root node (target chemical), and at each level,
        use UCB to score each of the options which can be taken. It will take
        the optimal option, which may be a new template application, or an
        already explored reaction. For the latter, it will descend to the next
        level and repeat the process until a new template application is chosen.
        """
        chem_path = [self.target]
        rxn_path = []
        template = None
        while template is None:
            leaf = chem_path[-1]
            options, template_opt = self.ucb(leaf, chem_path, exploration_weight=self.exploration_weight)

            if self.tree.out_degree(leaf) < self.max_branching:
                options.extend(template_opt)

            if not options:
                import pdb; pdb.set_trace()

            # Get the best option
            score, task = sorted(options, key=lambda x: x[0], reverse=True)[0]

            if isinstance(task, str):
                # This is an already explored reaction, so we need to descend the tree
                rxn_path.append(task)
                # If there are multiple reactants, pick the one with the lower visit count
                precursor = min((c for c in self.tree.successors(task) if not self.is_chemical_done(c)),
                                key=lambda x: self.tree.nodes[x]['visit_count'])
                chem_path.append(precursor)
            else:
                # This is a new template to apply
                template = task

        return chem_path, rxn_path, template

    def ucb(self, node, path, exploration_weight):
        """
        Calculate UCB score for all exploration options from the specified node.

        This algorithm considers both explored and unexplored template
        applications as potential routes for further exploration.

        Returns a list of (score, option) tuples sorted by score.
        """
        max_average_reward = 0

        reaction_options = []
        template_options = []

        templates = self.tree.nodes[node]['templates']
        explored = self.tree.nodes[node]['explored']
        visit_count = self.tree.nodes[node]['visit_count']

        # Get scores for explored templates (reaction node exists)
        for rxn in self.tree.successors(node):
            rxn_data = self.tree.nodes[rxn]

            if self.is_reaction_done(rxn) or len(set(self.tree.successors(rxn)) & set(path)) > 0:
                continue

            average_reward = rxn_data['reward_avg']
            max_average_reward = max(max_average_reward, average_reward)

            q_sa = -average_reward
            template_probability = sum([templates[t] for t in rxn_data['templates']])
            u_sa = template_probability * visit_count / (1 + rxn_data['visit_count'])

            score = q_sa + exploration_weight * u_sa

            # The options here are to follow a reaction down one level
            reaction_options.append((score, rxn))

        # Get scores for unexplored templates
        for template in templates:
            if template not in explored:
                q_sa = -(max_average_reward + 0.1)
                u_sa = templates[template] * (1 + np.sqrt(visit_count))
                score = q_sa + exploration_weight * u_sa

                # The options here are to apply a new template to this chemical
                template_options.append((score, template))

        if not reaction_options and not template_options:
            import pdb; pdb.set_trace()

        # Sort options from highest to lowest score
        reaction_options.sort(key=lambda x: x[0], reverse=True)
        template_options.sort(key=lambda x: x[0], reverse=True)

        return reaction_options, template_options

    def _get_precursors(self, chemical, template_idx):
        """
        Get all precursors from applying a template to a chemical.
        """
        mol = Chem.MolFromSmiles(chemical)
        smiles = Chem.MolToSmiles(mol, isomericSmiles=True)
        mol = rdchiralReactants(smiles)

        template = self.retro_transformer.get_one_template_by_idx(template_idx)
        try:
            template['rxn'] = rdchiralReaction(template['reaction_smarts'])
        except ValueError:
            return []

        outcomes = self.retro_transformer.apply_one_template(mol, template)

        precursors = [o['smiles_split'] for o in outcomes]

        return precursors

    def _process_precursors(self, target, template, precursors, path):
        """
        Process a list of precursors:
        1. Filter precursors by fast filter score
        2. Create and register Chemical objects for each new precursor
        3. Generate template relevance probabilities
        4. Create and register Reaction objects
        """
        for reactant_list in precursors:
            # Check if this precursor meets the fast filter score threshold
            reactant_smiles = '.'.join(reactant_list)
            score = self.retro_transformer.fast_filter(reactant_smiles, target)
            if score < self.fast_filter_threshold:
                continue

            for reactant in reactant_list:
                # TODO: Check banned molecules
                if False:
                    break

                if reactant in path:
                    # Avoid cycles
                    break

                if reactant not in self.chemicals:
                    # This is new, so create a Chemical node
                    self.create_chemical_node(reactant)
            else:
                reaction_smiles = reactant_smiles + '>>' + target
                template_score = self.tree.nodes[target]['templates'][template]

                # TODO: Check banned reactions
                if False:
                    continue

                if reaction_smiles in self.reactions:
                    # This reaction already exists
                    rxn_data = self.tree.nodes[reaction_smiles]
                    rxn_data['templates'].append(template)
                    rxn_data['template_score'] = max(rxn_data['template_score'], template_score)
                else:
                    # This is new, so create a Reaction node
                    self.reactions.append(reaction_smiles)
                    self.tree.add_node(
                        reaction_smiles,
                        fast_filter_score=score,
                        reward_avg=0.,
                        reward_tot=0.,
                        template_score=template_score,
                        templates=[template],
                        type='reaction',
                        visit_count=0,
                    )

                # Add edges to connect target -> reaction -> precursors
                self.tree.add_edge(target, reaction_smiles)
                for reactant in reactant_list:
                    self.tree.add_edge(reaction_smiles, reactant)

    def create_chemical_node(self, smiles):
        """
        Create a new chemical node from the provide SMILES and populate node
        properties with chemical data.

        Includes template relevance probabilities and purchase price.
        """
        template_prioritizer = self.retro_transformer.template_prioritizer
        probs, indices = template_prioritizer.predict(
            smiles,
            self.template_max_count,
            self.template_max_cum_prob
        )
        templates = {i: p for i, p in zip(indices, probs)}

        purchase_price = self.pricer.lookup_smiles(smiles, alreadyCanonical=False)

        terminal = self.is_terminal(smiles, purchase_price)

        self.chemicals.append(smiles)
        self.tree.add_node(
            smiles,
            explored=[],
            min_depth=None,
            purchase_price=purchase_price,
            reward_avg=0.,
            reward_tot=0.,
            templates=templates,
            terminal=terminal,
            type='chemical',
            visit_count=0,
        )

        self.is_chemical_done(smiles, update=True)

    def is_terminal(self, smiles, ppg):
        """
        Determine if the specified chemical is a terminal node in the tree based
        on pre-specified criteria.

        The current setup uses ppg as a mandatory criteria, with atom counts and
        chemical history data being optional, additional criteria.

        Args:
            smiles (str): smiles string of the chemical
            ppg (float): cost of the chemical
            hist (dict): historian data for the chemical
        """
        # Default to False
        is_terminal = False

        if self.max_ppg is not None:
            is_buyable = ppg and (ppg <= self.max_ppg)
            is_terminal = is_buyable

        # if self.max_natom_dict is not None:
        #     # Get structural properties
        #     mol = Chem.MolFromSmiles(smiles)
        #     if mol:
        #         natom_dict = defaultdict(lambda: 0)
        #         for a in mol.GetAtoms():
        #             natom_dict[a.GetSymbol()] += 1
        #         natom_dict['H'] = sum(a.GetTotalNumHs() for a in mol.GetAtoms())
        #         is_small_enough = all(natom_dict[k] <= v for k, v in self.max_natom_dict.items() if k != 'logic')
        #
        #         if self.max_natom_dict['logic'] == 'or':
        #             is_terminal = is_terminal or is_small_enough
        #         elif self.max_natom_dict['logic'] == 'and':
        #             is_terminal = is_terminal and is_small_enough
        #
        # if self.min_chemical_history_dict is not None:
        #     is_popular_enough = hist['as_reactant'] >= self.min_chemical_history_dict['as_reactant'] or \
        #                         hist['as_product'] >= self.min_chemical_history_dict['as_product']
        #
        #     if self.min_chemical_history_dict['logic'] == 'or':
        #         is_terminal = is_terminal or is_popular_enough
        #     elif self.min_chemical_history_dict['logic'] == 'and':
        #         is_terminal = is_terminal and is_popular_enough

        return is_terminal

    def get_buyable_paths(self, fmt='json'):
        """
        Return list of paths to buyables starting from the target node.
        """
        def _validate_path(_path):
            """Return true if all leaves are terminal."""
            leaves = (v for v, d in _path.out_degree() if d == 0)
            return all(_path.nodes[v]['terminal'] for v in leaves)

        tree = self.to_branching()
        target = [n for n, s in tree.nodes(data='source') if s == self.target][0]

        paths = (path for path in get_paths(tree, target, max_depth=self.max_depth) if _validate_path(path))

        if fmt == 'json':
            paths = [nx.tree_data(path, target) for path in paths]
        elif fmt == 'graph':
            paths = list(paths)
        else:
            raise ValueError('Unrecognized format type {0}'.format(fmt))

        return paths


def get_paths(tree, root, max_depth=None):
    """
    Return generator of all paths from the root node as `nx.DiGraph` objects.

    Designed for true tree where each node only has one parent.
    All node attributes are copied to the output paths.
    """
    def get_chem_paths(_node, _depth=0):
        """
        Return generator of paths with current node as the root.
        """
        if tree.out_degree(_node) == 0 or max_depth is not None and _depth >= max_depth:
            sub_path = nx.DiGraph()
            sub_path.add_node(_node, **tree.nodes[_node])
            yield sub_path
        else:
            for rxn in tree.successors(_node):
                for sub_path in get_rxn_paths(rxn, _depth + 1):
                    sub_path.add_node(_node, **tree.nodes[_node])
                    sub_path.add_edge(_node, rxn)
                    yield sub_path

    def get_rxn_paths(_node, _depth=0):
        """
        Return generator of paths with current node as root.
        """
        for path_combo in itertools.product(*(get_chem_paths(c, _depth) for c in tree.successors(_node))):
            sub_path = nx.union_all(path_combo)
            sub_path.add_node(_node, **tree.nodes[_node])
            for c in tree.successors(_node):
                sub_path.add_edge(_node, c)
            yield sub_path

    for path in get_chem_paths(root):
        yield path

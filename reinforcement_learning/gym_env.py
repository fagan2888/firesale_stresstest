import random
import matplotlib.pyplot as plt
import numpy as np

from economicsl import Simulation

from contracts import SellAsset, Tradable, Loan, AssetType
from behaviours import pay_off_liabilities
from institutions import Bank, DefaultException
from markets import AssetMarket
from model import Model

class RLBank(Bank):
    def __init__(self, name, simulation):
        super().__init__(name, simulation)
        self.time_of_death = None
        self.initial_bs = None

    def choose_actions(self, action):
        # 0) If I'm insolvent, default.
        if self.is_insolvent():
            raise DefaultException(self, 'SOLVENCY')
        balance = self.get_cash()
        # 1. Pay off liabilities to delever
        amountToDeLever = self.leverageConstraint.get_amount_to_delever()
        if amountToDeLever > 0:
            deLever = pay_off_liabilities(self, min(amountToDeLever, balance))
            balance -= deLever
            amountToDeLever -= deLever

        # 2. Raise liquidity to delever later
        sellAssetActions = self.get_all_actions_of_type(SellAsset)
        for saa in sellAssetActions:
            if saa.asset.assetType in action:
                rl_action_amount = action[saa.asset.assetType] * saa.asset.get_valuation('A')
                amount = min(rl_action_amount, saa.get_max())
                saa.set_amount(amount)
                if amount > 0:
                    saa.perform()

    def act(self, action):
        if not self.alive:
            return
        self.availableActions = self.get_available_actions()
        try:
            self.choose_actions(action)
        except DefaultException:
            # In general, when a bank defaults, its default treatment
            # may be order-dependent if executed immediately (e.g. when
            # it performs bilateral pull funding in the full model), so
            # it is best to delay it to the step() stage.
            self.do_trigger_default = True
            self.alive = False
            self.model.DefaultBanks.append(self.get_name())
            self.time_of_death = self.get_time()
            # This is for record keeping.
            self.simulation.bank_defaults_this_round += 1

    def observe(self):
        ldg = self.get_ledger()
        cb = ldg.get_asset_valuation_of(Tradable, 1)
        gb = ldg.get_asset_valuation_of(Tradable, 2)
        loan = ldg.get_asset_valuation_of(Loan)
        lev_ratio = self.leverageConstraint.get_leverage()
        prices = dict(self.model.assetMarket.prices)
        return prices, {'CB': cb, 'GB': gb}, {'LOAN': loan}, lev_ratio, self.initial_bs

class RLModelEnv(Model):
    def reset(self):
        self.simulation = Simulation()
        self.DefaultBanks = []
        self.allAgents = []
        self.allAgentBanks = {}
        self.assetMarket = AssetMarket(self)
        obs = {}
        #with open('simple_balance_sheet.csv', 'r') as data:
        with open('EBA_2018.csv', 'r') as data:
            self.bank_balancesheets = data.read().strip().split('\n')[1:]
        for bs in self.bank_balancesheets:
            # This steps consist of loading balance sheet from data file
            row = bs.split(' ')
            bank_name, CET1E, leverage, debt_sec, gov_bonds = row
            bank = RLBank(bank_name, self.simulation)
            debt_sec = float(debt_sec)
            gov_bonds = eval(gov_bonds)
            CET1E = float(CET1E)
            corp_bonds = debt_sec - gov_bonds
            lev_ratio = float(leverage) / 100
            asset = CET1E / lev_ratio
            cash = 0.05 * asset
            liability = asset - CET1E
            other_asset = asset - debt_sec - cash
            loan = other_liability = liability / 2
            bank.init(
                self, self.assetMarket,
                assets=(cash, corp_bonds, gov_bonds, other_asset),
                liabilities=(loan, other_liability))
            bank.get_ledger().set_initial_valuations()  # to calculate initial equity
            self.allAgents.append(bank)
            # RL-specific
            bank.initial_bs = {'CB': corp_bonds, 'GB': gov_bonds, 'LOAN': loan}
            obs[bank_name] = bank.observe()
            self.allAgentBanks[bank_name] = bank
        self.apply_initial_shock(
            self.parameters.ASSET_TO_SHOCK,
            self.parameters.INITIAL_SHOCK)
        return obs

    def step(self, action_dict):
        """Returns observations from ready agents.
        The returns are dicts mapping from agent_id strings to values. The
        number of agents in the env can vary over time.
        Returns
        -------
            obs (dict): New observations for each ready agent.
            rewards (dict): Reward values for each ready agent. If the
                episode is just started, the value will be None.
            dones (dict): Done values for each ready agent. The special key
                "__all__" (required) is used to indicate env termination.
            infos (dict): Optional info values for each agent id.
        """
        obs, rewards, dones, infos = {}, {}, {}, {}
        self.simulation.advance_time()
        self.simulation.bank_defaults_this_round = 0
        random.shuffle(self.allAgents)
        for agent in self.allAgents:
            agent.step()
        for name, agent in self.allAgentBanks.items():
            if not agent.alive:
                continue
            action = action_dict[name]
            agent.act(action)
            # for observation
            obs[name] = agent.observe()
            if agent.alive:
                ldg = agent.get_ledger()
                rewards[name] = 1 + ldg.get_equity_valuation() / ldg.get_initial_equity()
                dones[name] = False
            else:
                rewards[name] = -10
                dones[name] = True
        if self.parameters.SIMULTANEOUS_FIRESALE:
            self.assetMarket.clear_the_market()
        new_prices = dict(self.assetMarket.prices)
        infos['ASSET_PRICES'] = new_prices
        infos['NUM_DEFAULTS'] = len(self.DefaultBanks)
        now = self.get_time()
        infos['AVERAGE_LIFESPAN'] = sum(now if a.alive else a.time_of_death for a in self.allAgents) / len(self.allAgents)
        infos['TOTAL_EQUITY'] = 0
        for bank in self.allAgents:
            if not bank.alive:
                continue
            infos['TOTAL_EQUITY'] += bank.get_ledger().get_equity_valuation()

        return obs, rewards, dones, infos

if __name__ == '__main__':
    random.seed(1337)
    np.random.seed(1337)
    env = RLModelEnv()
    initial_obs = env.reset()

    def stupid_action(bank):
        action = {}
        action[AssetType.CORPORATE_BONDS], action[AssetType.GOV_BONDS] = 0.2 * abs(np.random.normal() - 0.5), 0.2 * np.random.normal() * abs(np.random.normal() - 0.5)
        return action

    play, max_play = 0, 10
    num_defaults = []
    while play < max_play:
        actions = {}
        play += 1
        for bank_name, bank in env.allAgentBanks.items():
            actions[bank_name] = stupid_action(bank)  # this is where you use your RLAgents!
        obs, _, _, infos = env.step(actions)
        num_defaults.append(infos['NUM_DEFAULTS'])

    plt.plot(num_defaults)
    plt.ylabel('Number of defaults')
    plt.show()

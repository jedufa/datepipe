from __future__ import annotations

from pathlib import Path
import json
import subprocess


def test_find_dates_accepts_proposal_array_and_orders_by_saved_preferences():
  """Exercise the async handler that previously crashed at browser startup."""
  script = r'''
const fs = require('node:fs');
const vm = require('node:vm');
const app = fs.readFileSync('static/app.js', 'utf8');
const startup = app.lastIndexOf('\nloadState().catch');
if (startup < 0) throw new Error('Startup entry point not found');
const context = vm.createContext({
  console,
  document: {querySelector: () => ({}), addEventListener: () => {}},
  localStorage: {getItem: () => null},
});
vm.runInContext(app.slice(0, startup), context);
vm.runInContext(`
state.feedback = {'category:cultural': {keep: 2, remove: 0}};
currentFormValues = () => ({homeBase: 'Vienna', searchDate: '2026-10-19'});
selectedSlot = () => ({id: 'synthetic-slot'});
api = async () => ({source: 'local', options: [
  {id: 'music', category: 'social', stretch: 'familiar', sourceIdeaId: 'demo-a'},
  {id: 'gallery', category: 'cultural', stretch: 'familiar', sourceIdeaId: 'demo-b'},
]});
renderCityResearch = () => {};
loadResearchReview = async () => {};
applyFilters = () => {};
`, context);
vm.runInContext(`findDates().then(() => console.log(JSON.stringify({
  isArray: Array.isArray(state.allProposals),
  ids: state.allProposals.map(p => p.id),
})))`, context);
'''
  result = subprocess.run(['node', '-e', script], capture_output=True, text=True, check=True)
  payload = json.loads(result.stdout)
  assert payload == {'isArray': True, 'ids': ['gallery', 'music']}

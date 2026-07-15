import test from 'node:test'
import assert from 'node:assert/strict'

function hasNonEmptyArray(value) {
  return Array.isArray(value) && value.length > 0
}

function hasEffectiveRequirementGraph(value) {
  return Boolean(value && typeof value === 'object' && (
    hasNonEmptyArray(value.requirements) ||
    hasNonEmptyArray(value.function_items) ||
    hasNonEmptyArray(value.dataflow_edges)
  ))
}

function buildMinimalRequirementGraph({ functionItems, responsibilityEdges } = {}) {
  if (!hasNonEmptyArray(functionItems) && !hasNonEmptyArray(responsibilityEdges)) return null
  return {
    requirements: [],
    function_items: hasNonEmptyArray(functionItems) ? functionItems : [],
    dataflow_edges: hasNonEmptyArray(responsibilityEdges) ? responsibilityEdges : [],
  }
}

function createHarness() {
  const state = {
    pendingFunctionItems: [],
    pendingResponsibilityEdges: [],
    resolvedFunctionItems: null,
    resolvedResponsibilityEdges: null,
    resolvedRequirementGraph: null,
    creationPlan: null,
  }
  function saveResolvedGraphSnapshot({ functionItems, responsibilityEdges } = {}) {
    if (hasNonEmptyArray(functionItems) || (Array.isArray(functionItems) && !Array.isArray(state.resolvedFunctionItems))) {
      state.resolvedFunctionItems = functionItems
    }
    if (hasNonEmptyArray(responsibilityEdges) || (Array.isArray(responsibilityEdges) && !Array.isArray(state.resolvedResponsibilityEdges))) {
      state.resolvedResponsibilityEdges = responsibilityEdges
    }
  }
  function saveResolvedRequirementGraphSnapshot({ requirementGraph, functionItems, responsibilityEdges } = {}) {
    if (hasEffectiveRequirementGraph(requirementGraph)) {
      state.resolvedRequirementGraph = requirementGraph
      return
    }
    const minimalGraph = buildMinimalRequirementGraph({ functionItems, responsibilityEdges })
    if (minimalGraph) state.resolvedRequirementGraph = minimalGraph
  }
  function mergeFinalPlanGraph(plan) {
    const finalFunctionItems = hasNonEmptyArray(plan.function_items)
      ? plan.function_items
      : (Array.isArray(state.resolvedFunctionItems) ? state.resolvedFunctionItems : state.pendingFunctionItems)
    const finalResponsibilityEdges = hasNonEmptyArray(plan.responsibility_edges)
      ? plan.responsibility_edges
      : (Array.isArray(state.resolvedResponsibilityEdges) ? state.resolvedResponsibilityEdges : state.pendingResponsibilityEdges)
    const finalRequirementGraph = hasEffectiveRequirementGraph(plan.requirement_graph)
      ? plan.requirement_graph
      : (hasEffectiveRequirementGraph(state.resolvedRequirementGraph) ? state.resolvedRequirementGraph : null)
    return { ...plan, function_items: finalFunctionItems, responsibility_edges: finalResponsibilityEdges, requirement_graph: finalRequirementGraph }
  }
  return { state, saveResolvedGraphSnapshot, saveResolvedRequirementGraphSnapshot, mergeFinalPlanGraph }
}

test('empty final graph does not overwrite resolved graph', () => {
  const h = createHarness()
  const oldNode = { target_file: 'scripts/old.py', role: 'processor' }
  const oldEdge = { from_node: 'input', to_node: 'scripts/old.py' }
  h.saveResolvedGraphSnapshot({ functionItems: [oldNode], responsibilityEdges: [oldEdge] })
  h.saveResolvedRequirementGraphSnapshot({ functionItems: [oldNode], responsibilityEdges: [oldEdge] })
  h.saveResolvedGraphSnapshot({ functionItems: [], responsibilityEdges: [] })

  h.state.creationPlan = h.mergeFinalPlanGraph({ function_items: [], responsibility_edges: [], requirement_graph: null })

  assert.deepEqual(h.state.resolvedFunctionItems, [oldNode])
  assert.deepEqual(h.state.resolvedResponsibilityEdges, [oldEdge])
  assert.deepEqual(h.state.creationPlan.function_items, [oldNode])
  assert.deepEqual(h.state.creationPlan.responsibility_edges, [oldEdge])
  assert.ok(h.state.creationPlan.requirement_graph)
})

test('non-empty final graph overwrites older resolved graph', () => {
  const h = createHarness()
  const oldNode = { target_file: 'scripts/old.py' }
  const oldEdge = { from_node: 'input', to_node: 'scripts/old.py' }
  const newNode = { target_file: 'scripts/new.py' }
  const newEdge = { from_node: 'input', to_node: 'scripts/new.py' }
  const newGraph = { requirements: [], function_items: [newNode], dataflow_edges: [newEdge] }

  h.saveResolvedGraphSnapshot({ functionItems: [oldNode], responsibilityEdges: [oldEdge] })
  h.saveResolvedRequirementGraphSnapshot({ functionItems: [oldNode], responsibilityEdges: [oldEdge] })
  h.state.creationPlan = h.mergeFinalPlanGraph({ function_items: [newNode], responsibility_edges: [newEdge], requirement_graph: newGraph })

  assert.deepEqual(h.state.creationPlan.function_items, [newNode])
  assert.deepEqual(h.state.creationPlan.responsibility_edges, [newEdge])
  assert.deepEqual(h.state.creationPlan.requirement_graph, newGraph)
})

"""LangGraph 토폴로지 선언만 둔다."""

from langgraph.graph import END, START, StateGraph

from app.agents.flow.stages import (
    WorkflowContext,
    WorkflowState,
    analysis,
    coordinator,
    data_design,
    db_validation,
    delivery,
    function_design,
    guarded,
    render,
    requirements_review,
    semantic_review,
    structure_validation,
)


def _route(state: WorkflowState):
    return END if state["stop"] else state["phase"]


_nodes = {
    "analysis": analysis,
    "requirements_review": requirements_review,
    "function_design": function_design,
    "data_design": data_design,
    "structure_validation": structure_validation,
    "render": render,
    "semantic_review": semantic_review,
    "db_validation": db_validation,
    "delivery": delivery,
}


async def _coordinator(state, runtime):
    return await coordinator(state, runtime)


builder = StateGraph(WorkflowState, context_schema=WorkflowContext)
builder.add_node("coordinator", _coordinator)
for name, handler in _nodes.items():

    async def wrapped(state, runtime, _handler=handler):
        return await guarded(_handler, state, runtime)

    builder.add_node(name, wrapped)
builder.add_edge(START, "coordinator")
builder.add_conditional_edges("coordinator", _route, [*_nodes, END])
for name in _nodes:
    builder.add_edge(name, "coordinator")
workflow_graph = builder.compile()

#include <pybind11/pybind11.h>

namespace py = pybind11;

PYBIND11_MODULE(agent_native, m) {
    m.doc() = "agent native extension";
    m.def("ping", []() { return "pong"; });
}
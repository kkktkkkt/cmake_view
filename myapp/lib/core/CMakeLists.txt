add_library(core_lib STATIC
    core.cpp
    utils.cpp
)

target_include_directories(core_lib
    PUBLIC
        ${CMAKE_SOURCE_DIR}/lib/core
)

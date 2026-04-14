add_library(net_lib SHARED
    http.cpp
)

target_link_libraries(net_lib
    PRIVATE
        core_lib
        OpenSSL::SSL
)

target_include_directories(net_lib
    PRIVATE
        ${CMAKE_CURRENT_SOURCE_DIR}
    INTERFACE
        ${CMAKE_SOURCE_DIR}/lib/net/include
)

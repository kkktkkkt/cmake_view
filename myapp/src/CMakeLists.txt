add_executable(myapp
    main.cpp
    app.cpp
)

target_link_libraries(myapp
    PRIVATE
        core_lib
        net_lib
)

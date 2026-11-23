cmake_minimum_required(VERSION 4.0)
project(Heliograph LANGUAGES C)
include("${IMPOSSIBLE_SDK_DIR}/cmake/ImpossibleSDK.cmake")
include_directories(engine)
set(STORE engine/base.c engine/store.c)
set(CORE ${STORE} engine/helio.c)

impossible_add_program(helio CONSOLE TIER 0 SOURCES src/main.c src/plat_windows.c ${CORE})
impossible_add_program(racer CONSOLE TIER 0 SOURCES tests/racer.c tests/racer_windows.c src/plat_windows.c ${STORE})

# WSL sessions use the native program, so they never wait for WSL's bridge to Windows.
if(CMAKE_HOST_SYSTEM_NAME STREQUAL "Linux")
    file(GLOB headers CONFIGURE_DEPENDS engine/*.h)
    function(add_linux_program name)
        set(program "${CMAKE_BINARY_DIR}/linux/${name}")
        set(sources)
        foreach(source IN LISTS ARGN)
            list(APPEND sources "${CMAKE_SOURCE_DIR}/${source}")
        endforeach()
        add_custom_command(OUTPUT "${program}"
            COMMAND "${CMAKE_COMMAND}" -E make_directory "${CMAKE_BINARY_DIR}/linux"
            COMMAND cc -std=gnu23 -O2 -Wall -Wextra -Werror -fstack-protector-strong -D_FORTIFY_SOURCE=2
                -I "${CMAKE_SOURCE_DIR}/engine" ${sources} -o "${program}"
            DEPENDS ${sources} ${headers} VERBATIM)
        add_custom_target(${name}-linux ALL DEPENDS "${program}")
    endfunction()
    add_linux_program(helio src/main_linux.c src/plat_linux.c ${CORE})
    add_linux_program(racer tests/racer.c tests/racer_linux.c src/plat_linux.c ${STORE})
endif()

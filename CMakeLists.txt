cmake_minimum_required(VERSION 4.0)
project(Heliograph LANGUAGES C)
include("${IMPOSSIBLE_SDK_DIR}/cmake/ImpossibleSDK.cmake")
include_directories(engine)
set(ENGINE engine/engine.c)

impossible_add_program(Heliograph CONSOLE TIER 0 SOURCES src/main.c ${ENGINE})
impossible_add_program(engine-tests CONSOLE TIER 0 SOURCES tests/engine_tests.c ${ENGINE})

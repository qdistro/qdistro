# Minimal CLI11 package config for the vendored single-header CLI11.
# Tumbleweed does not ship a cli11 package; quickshell's
# src/launch/CMakeLists.txt does find_package(CLI11 CONFIG REQUIRED)
# and links the imported target CLI11::CLI11. Point CLI11_DIR or
# CMAKE_PREFIX_PATH at this directory.
if(NOT TARGET CLI11::CLI11)
    add_library(CLI11::CLI11 INTERFACE IMPORTED)
    get_filename_component(_cli11_root "${CMAKE_CURRENT_LIST_DIR}/.." ABSOLUTE)
    set_target_properties(CLI11::CLI11 PROPERTIES
        INTERFACE_INCLUDE_DIRECTORIES "${_cli11_root}/third-party/cli11/include"
    )
    unset(_cli11_root)
endif()
set(CLI11_FOUND TRUE)

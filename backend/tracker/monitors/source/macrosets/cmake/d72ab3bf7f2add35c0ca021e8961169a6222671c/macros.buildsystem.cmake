
# Example buildsystem for cmake
%buildsystem_cmake_conf() %cmake %*
%buildsystem_cmake_build() %cmake_build %*
%buildsystem_cmake_install() %cmake_install %*
%buildsystem_cmake_check() %ctest %*
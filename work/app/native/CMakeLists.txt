cmake_minimum_required(VERSION 3.21)
project(LCSC3DAltium LANGUAGES CXX)
set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
find_package(Qt6 REQUIRED COMPONENTS Core Gui)
set(VENDOR "${CMAKE_CURRENT_SOURCE_DIR}/vendor/EasyKiConverter/src")
add_executable(lcsc-altium
    main.cpp
    ${VENDOR}/models/SymbolData.cpp
    ${VENDOR}/models/SymbolDataSerializer.cpp
    ${VENDOR}/models/SymbolPinSerializer.cpp
    ${VENDOR}/models/SymbolShapeSerializer.cpp
    ${VENDOR}/models/FootprintData.cpp
    ${VENDOR}/models/FootprintDataSerializer.cpp
    ${VENDOR}/models/Model3DData.cpp
    ${VENDOR}/core/easyeda/EasyedaSymbolImporter.cpp
    ${VENDOR}/core/easyeda/EasyedaFootprintImporter.cpp
    ${VENDOR}/core/utils/SvgPathParser.cpp
    ${VENDOR}/core/ir/EasyedaLayerMap.cpp
    ${VENDOR}/core/ir/SymbolIR.cpp
    ${VENDOR}/core/altium/ExporterAltiumSymbol.cpp
    ${VENDOR}/core/altium/ExporterAltiumFootprint.cpp
    ${VENDOR}/core/altium/compound/OLECompoundWriter.cpp
    ${VENDOR}/core/altium/utils/AltiumBinaryWriter.cpp
    ${VENDOR}/core/altium/utils/AltiumLayerMap.cpp
    ${VENDOR}/core/altium/writers/AltiumSchLibWriter.cpp
    ${VENDOR}/core/altium/writers/AltiumPcbLibWriter.cpp
)
target_include_directories(lcsc-altium PRIVATE
    ${VENDOR} ${VENDOR}/models ${VENDOR}/core/utils ${VENDOR}/core/altium)
target_link_libraries(lcsc-altium PRIVATE Qt6::Core Qt6::Gui)
if(MSVC)
    target_compile_options(lcsc-altium PRIVATE /utf-8)
endif()

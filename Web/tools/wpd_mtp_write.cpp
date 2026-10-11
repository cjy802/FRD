#include <windows.h>
#include <portabledeviceapi.h>
#include <portabledevicetypes.h>
#include <portabledevice.h>
#include <wrl/client.h>

#include <filesystem>
#include <fstream>
#include <iostream>
#include <stdexcept>
#include <string>
#include <vector>

using Microsoft::WRL::ComPtr;
namespace fs = std::filesystem;

struct HResultError : std::runtime_error {
    HRESULT hr;
    HResultError(const std::string& message, HRESULT value)
        : std::runtime_error(message), hr(value) {}
};

static void Check(HRESULT hr, const char* operation) {
    if (FAILED(hr)) {
        throw HResultError(operation, hr);
    }
}

static std::wstring GetStringValue(
    IPortableDeviceProperties* properties,
    const std::wstring& objectId,
    REFPROPERTYKEY key) {
    ComPtr<IPortableDeviceKeyCollection> keys;
    Check(CoCreateInstance(
        CLSID_PortableDeviceKeyCollection,
        nullptr,
        CLSCTX_INPROC_SERVER,
        IID_PPV_ARGS(&keys)),
        "CoCreateInstance(CLSID_PortableDeviceKeyCollection)");
    Check(keys->Add(key), "IPortableDeviceKeyCollection::Add");

    ComPtr<IPortableDeviceValues> values;
    Check(properties->GetValues(objectId.c_str(), keys.Get(), &values),
        "IPortableDeviceProperties::GetValues");

    LPWSTR value = nullptr;
    Check(values->GetStringValue(key, &value),
        "IPortableDeviceValues::GetStringValue");
    std::wstring result(value ? value : L"");
    CoTaskMemFree(value);
    return result;
}

static std::wstring FindChild(
    IPortableDeviceContent* content,
    IPortableDeviceProperties* properties,
    const std::wstring& parentId,
    const std::wstring& childName) {
    ComPtr<IEnumPortableDeviceObjectIDs> enumerator;
    Check(content->EnumObjects(0, parentId.c_str(), nullptr, &enumerator),
        "IPortableDeviceContent::EnumObjects");

    while (true) {
        LPWSTR ids[16] = {};
        ULONG fetched = 0;
        HRESULT hr = enumerator->Next(16, ids, &fetched);
        if (hr == S_FALSE || fetched == 0) {
            break;
        }
        Check(hr, "IEnumPortableDeviceObjectIDs::Next");

        for (ULONG i = 0; i < fetched; ++i) {
            std::wstring objectId(ids[i] ? ids[i] : L"");
            std::wstring name;
            try {
                name = GetStringValue(properties, objectId, WPD_OBJECT_NAME);
            } catch (...) {
                CoTaskMemFree(ids[i]);
                throw;
            }
            CoTaskMemFree(ids[i]);
            if (name == childName) {
                return objectId;
            }
        }
    }

    throw std::runtime_error("MTP object not found");
}

static std::wstring FindDevice(IPortableDeviceManager* manager, const std::wstring& wantedName) {
    DWORD count = 0;
    Check(manager->GetDevices(nullptr, &count),
        "IPortableDeviceManager::GetDevices(count)");
    std::vector<LPWSTR> ids(count);
    if (count == 0) {
        throw std::runtime_error("No WPD device is connected");
    }
    Check(manager->GetDevices(ids.data(), &count),
        "IPortableDeviceManager::GetDevices");

    for (DWORD i = 0; i < count; ++i) {
        DWORD chars = 0;
        manager->GetDeviceFriendlyName(ids[i], nullptr, &chars);
        std::vector<wchar_t> buffer(chars ? chars : 1);
        if (chars > 0) {
            Check(manager->GetDeviceFriendlyName(ids[i], buffer.data(), &chars),
                "IPortableDeviceManager::GetDeviceFriendlyName");
        }
        std::wstring name(buffer.data());
        if (name == wantedName) {
            std::wstring result(ids[i]);
            for (LPWSTR id : ids) {
                CoTaskMemFree(id);
            }
            return result;
        }
    }

    for (LPWSTR id : ids) {
        CoTaskMemFree(id);
    }
    throw std::runtime_error("Requested WPD device was not found");
}

static void WriteExistingObject(
    IPortableDeviceResources* resources,
    const std::wstring& objectId,
    const fs::path& sourcePath) {
    DWORD optimalBufferSize = 0;
    ComPtr<IStream> destination;
    Check(resources->GetStream(
        objectId.c_str(),
        WPD_RESOURCE_DEFAULT,
        STGM_WRITE | STGM_SHARE_DENY_NONE,
        &optimalBufferSize,
        &destination),
        "IPortableDeviceResources::GetStream(write)");

    const auto sourceSize = fs::file_size(sourcePath);
    ULARGE_INTEGER size{};
    size.QuadPart = sourceSize;
    Check(destination->SetSize(size), "IStream::SetSize");

    LARGE_INTEGER origin{};
    Check(destination->Seek(origin, STREAM_SEEK_SET, nullptr), "IStream::Seek");

    std::ifstream input(sourcePath, std::ios::binary);
    if (!input) {
        throw std::runtime_error("Cannot open source KMZ");
    }
    const size_t bufferSize = optimalBufferSize ? optimalBufferSize : 1024 * 1024;
    std::vector<char> buffer(bufferSize);
    while (input) {
        input.read(buffer.data(), static_cast<std::streamsize>(buffer.size()));
        const std::streamsize read = input.gcount();
        if (read <= 0) {
            break;
        }
        ULONG written = 0;
        Check(destination->Write(buffer.data(), static_cast<ULONG>(read), &written),
            "IStream::Write");
        if (written != static_cast<ULONG>(read)) {
            throw std::runtime_error("Short WPD write");
        }
    }
    Check(destination->Commit(STGC_DEFAULT), "IStream::Commit");
}

static ComPtr<IPortableDeviceValues> CreateClientInfo() {
    ComPtr<IPortableDeviceValues> clientInfo;
    Check(CoCreateInstance(
        CLSID_PortableDeviceValues,
        nullptr,
        CLSCTX_INPROC_SERVER,
        IID_PPV_ARGS(&clientInfo)),
        "CoCreateInstance(CLSID_PortableDeviceValues)");
    Check(clientInfo->SetStringValue(WPD_CLIENT_NAME, L"DJI RC2 KMZ Agent"),
        "Set WPD_CLIENT_NAME");
    Check(clientInfo->SetUnsignedIntegerValue(WPD_CLIENT_MAJOR_VERSION, 1),
        "Set WPD_CLIENT_MAJOR_VERSION");
    Check(clientInfo->SetUnsignedIntegerValue(WPD_CLIENT_MINOR_VERSION, 0),
        "Set WPD_CLIENT_MINOR_VERSION");
    Check(clientInfo->SetUnsignedIntegerValue(WPD_CLIENT_REVISION, 1),
        "Set WPD_CLIENT_REVISION");
    Check(clientInfo->SetUnsignedIntegerValue(
        WPD_CLIENT_SECURITY_QUALITY_OF_SERVICE, SECURITY_IMPERSONATION),
        "Set WPD_CLIENT_SECURITY_QUALITY_OF_SERVICE");
    Check(clientInfo->SetUnsignedIntegerValue(
        WPD_CLIENT_DESIRED_ACCESS, GENERIC_READ | GENERIC_WRITE),
        "Set WPD_CLIENT_DESIRED_ACCESS");
    Check(clientInfo->SetUnsignedIntegerValue(
        WPD_CLIENT_SHARE_MODE, FILE_SHARE_READ | FILE_SHARE_WRITE),
        "Set WPD_CLIENT_SHARE_MODE");
    return clientInfo;
}

static int Run(int argc, wchar_t** argv) {
    if (argc != 5) {
        std::wcerr << L"Usage: wpd_mtp_write.exe <device> <relative-path> <file-name> <source-kmz>\n";
        return 2;
    }

    const std::wstring deviceName = argv[1];
    const std::wstring relativePath = argv[2];
    const std::wstring fileName = argv[3];
    const fs::path sourcePath = argv[4];
    if (!fs::is_regular_file(sourcePath)) {
        throw std::runtime_error("Source KMZ does not exist");
    }

    ComPtr<IPortableDeviceManager> manager;
    Check(CoCreateInstance(
        CLSID_PortableDeviceManager,
        nullptr,
        CLSCTX_INPROC_SERVER,
        IID_PPV_ARGS(&manager)),
        "CoCreateInstance(CLSID_PortableDeviceManager)");
    const std::wstring deviceId = FindDevice(manager.Get(), deviceName);

    ComPtr<IPortableDevice> device;
    Check(CoCreateInstance(
        CLSID_PortableDevice,
        nullptr,
        CLSCTX_INPROC_SERVER,
        IID_PPV_ARGS(&device)),
        "CoCreateInstance(CLSID_PortableDevice)");
    auto clientInfo = CreateClientInfo();
    Check(device->Open(deviceId.c_str(), clientInfo.Get()), "IPortableDevice::Open");

    ComPtr<IPortableDeviceContent> content;
    Check(device->Content(&content), "IPortableDevice::Content");
    ComPtr<IPortableDeviceProperties> properties;
    Check(content->Properties(&properties), "IPortableDeviceContent::Properties");

    std::wstring objectId = WPD_DEVICE_OBJECT_ID;
    size_t start = 0;
    while (start < relativePath.size()) {
        size_t end = relativePath.find_first_of(L"/\\", start);
        if (end == std::wstring::npos) {
            end = relativePath.size();
        }
        if (end > start) {
            objectId = FindChild(content.Get(), properties.Get(), objectId,
                relativePath.substr(start, end - start));
        }
        start = end + 1;
    }

    const std::wstring fileId = FindChild(content.Get(), properties.Get(), objectId, fileName);
    ComPtr<IPortableDeviceResources> resources;
    Check(content->Transfer(&resources), "IPortableDeviceContent::Transfer");
    WriteExistingObject(resources.Get(), fileId, sourcePath);
    std::wcout << L"WPD_WRITE_OK\n";
    return 0;
}

int wmain(int argc, wchar_t** argv) {
    HRESULT hr = CoInitializeEx(nullptr, COINIT_MULTITHREADED);
    if (FAILED(hr)) {
        std::wcerr << L"CoInitializeEx failed: 0x" << std::hex << hr << L"\n";
        return 1;
    }
    int exitCode = 1;
    try {
        exitCode = Run(argc, argv);
    } catch (const HResultError& error) {
        std::wcerr << L"WPD failure: 0x" << std::hex << error.hr << L" "
                   << error.what() << L"\n";
    } catch (const std::exception& error) {
        std::cerr << "WPD failure: " << error.what() << "\n";
    }
    CoUninitialize();
    return exitCode;
}

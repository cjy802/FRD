#include <windows.h>
#include <shobjidl_core.h>
#include <wrl/client.h>

#include <iostream>
#include <stdexcept>
#include <string>

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

static int Run(int argc, wchar_t** argv) {
    if (argc != 5) {
        std::wcerr << L"Usage: silent_mtp_replace.exe <source> <target-folder-parsing-path> <target-file-name> <target-file-parsing-path>\n";
        return 2;
    }

    const wchar_t* sourcePath = argv[1];
    const wchar_t* targetFolderPath = argv[2];
    const wchar_t* targetFileName = argv[3];
    const wchar_t* targetFilePath = argv[4];

    Microsoft::WRL::ComPtr<IShellItem> source;
    Microsoft::WRL::ComPtr<IShellItem> destinationFolder;
    Microsoft::WRL::ComPtr<IShellItem> existingTarget;
    Check(SHCreateItemFromParsingName(sourcePath, nullptr, IID_PPV_ARGS(&source)),
        "SHCreateItemFromParsingName(source)");
    Check(SHCreateItemFromParsingName(targetFolderPath, nullptr, IID_PPV_ARGS(&destinationFolder)),
        "SHCreateItemFromParsingName(target folder)");

    HRESULT targetResult = SHCreateItemFromParsingName(
        targetFilePath, nullptr, IID_PPV_ARGS(&existingTarget));
    const bool targetExists = SUCCEEDED(targetResult);

    Microsoft::WRL::ComPtr<IFileOperation> operation;
    Check(CoCreateInstance(
        CLSID_FileOperation,
        nullptr,
        CLSCTX_INPROC_SERVER,
        IID_PPV_ARGS(&operation)),
        "CoCreateInstance(CLSID_FileOperation)");

    constexpr DWORD silentFlags =
        FOF_SILENT | FOF_NOCONFIRMATION | FOF_NOERRORUI | FOF_NOCONFIRMMKDIR;
    Check(operation->SetOperationFlags(silentFlags), "IFileOperation::SetOperationFlags");

    if (targetExists) {
        Check(operation->DeleteItem(existingTarget.Get(), nullptr),
            "IFileOperation::DeleteItem");
    }
    Check(operation->CopyItem(source.Get(), destinationFolder.Get(), targetFileName, nullptr),
        "IFileOperation::CopyItem");
    Check(operation->PerformOperations(), "IFileOperation::PerformOperations");

    BOOL aborted = FALSE;
    Check(operation->GetAnyOperationsAborted(&aborted),
        "IFileOperation::GetAnyOperationsAborted");
    if (aborted) {
        throw std::runtime_error("MTP file operation was aborted");
    }
    std::wcout << L"SILENT_MTP_REPLACE_OK\n";
    return 0;
}

int wmain(int argc, wchar_t** argv) {
    HRESULT hr = CoInitializeEx(nullptr, COINIT_APARTMENTTHREADED);
    if (FAILED(hr)) {
        std::wcerr << L"CoInitializeEx failed: 0x" << std::hex << hr << L"\n";
        return 1;
    }
    int exitCode = 1;
    try {
        exitCode = Run(argc, argv);
    } catch (const HResultError& error) {
        std::wcerr << L"Silent MTP failure: 0x" << std::hex << error.hr << L" "
                   << error.what() << L"\n";
    } catch (const std::exception& error) {
        std::cerr << "Silent MTP failure: " << error.what() << "\n";
    }
    CoUninitialize();
    return exitCode;
}

package sass;

import java.io.IOException;

import ghidra.app.util.bin.ByteProvider;
import ghidra.formats.gfilesystem.FSRLRoot;
import ghidra.formats.gfilesystem.FileSystemService;
import ghidra.formats.gfilesystem.factory.GFileSystemFactoryByteProvider;
import ghidra.formats.gfilesystem.factory.GFileSystemProbeByteProvider;
import ghidra.util.exception.CancelledException;
import ghidra.util.task.TaskMonitor;

/** Cheap header probe; CUDA recovery only runs when the user opens the container. */
public class SassEngineFileSystemFactory
        implements GFileSystemFactoryByteProvider<SassEngineFileSystem>,
                   GFileSystemProbeByteProvider {
    @Override
    public boolean probe(ByteProvider provider, FileSystemService service, TaskMonitor monitor)
            throws IOException, CancelledException {
        return SassEngineParser.header(provider, monitor) != null;
    }

    @Override
    public SassEngineFileSystem create(FSRLRoot fsrl, ByteProvider provider,
            FileSystemService service, TaskMonitor monitor)
            throws IOException, CancelledException {
        var filesystem = new SassEngineFileSystem(fsrl, provider, service);
        try {
            filesystem.mount(monitor);
            return filesystem;
        }
        catch (IOException | CancelledException | RuntimeException e) {
            filesystem.close();
            throw e;
        }
    }
}

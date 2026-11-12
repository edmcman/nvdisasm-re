package sass;

import java.io.IOException;
import java.io.InputStream;
import java.util.Comparator;
import java.util.List;

import org.apache.commons.compress.compressors.lz4.BlockLZ4CompressorInputStream;

import ghidra.app.util.bin.ByteArrayProvider;
import ghidra.app.util.bin.ByteProvider;
import ghidra.app.util.bin.ByteProviderWrapper;
import ghidra.formats.gfilesystem.*;
import ghidra.formats.gfilesystem.annotations.FileSystemInfo;
import ghidra.formats.gfilesystem.fileinfo.FileAttributes;
import ghidra.util.exception.CancelledException;
import ghidra.util.task.TaskMonitor;

/** Kernel browser for serialized TensorRT engines, including Ultralytics wrappers. */
@FileSystemInfo(type = "sassengine", description = "TensorRT CUDA Kernel Browser",
    extensions = {"engine", "plan"}, factory = SassEngineFileSystemFactory.class)
public class SassEngineFileSystem implements GFileSystem {
    private ByteProvider provider;
    private final FSRLRoot fsrl;
    private final FileSystemService service;
    private final FileSystemRefManager refs = new FileSystemRefManager(this);
    private final FileSystemIndexHelper<Entry> index;
    private record Entry(byte[] bytes, SassEngineParser.Module module) {}

    public SassEngineFileSystem(FSRLRoot fsrl, ByteProvider provider, FileSystemService service) {
        this.fsrl = fsrl;
        this.provider = provider;
        this.service = service;
        index = new FileSystemIndexHelper<>(this, fsrl);
    }

    public void mount(TaskMonitor monitor) throws IOException, CancelledException {
        var result = SassEngineParser.scan(provider, this::decode, monitor);
        for (var module : result.modules()) {
            monitor.checkCancelled();
            index.storeFile(module.path(), -1, false, module.elf().size(), new Entry(null, module));
        }
        byte[] manifest = result.manifest();
        index.storeFile("manifest.json", -1, false, manifest.length, new Entry(manifest, null));
        if (result.header().metadata() != null) {
            byte[] metadata = result.header().metadata();
            index.storeFile("metadata.json", -1, false, metadata.length, new Entry(metadata, null));
        }
    }

    private ByteProvider decode(SassEngineParser.Source source, TaskMonitor monitor)
            throws IOException, CancelledException {
        monitor.checkCancelled();
        // The version in the cache key prevents reusing data after decoder rules change.
        String key = "sassengine-v1-lz4-" + source.offset() + "-" + source.storedSize() +
            "-" + source.decodedSize();
        ByteProvider decoded;
        try {
            decoded = service.getDerivedByteProviderPush(provider.getFSRL(), null, key,
                source.decodedSize(), output -> {
                    try (ByteProvider slice = new ByteProviderWrapper(provider, source.offset(),
                            source.storedSize());
                         InputStream input = slice.getInputStream(0);
                         var lz4 = new BlockLZ4CompressorInputStream(input)) {
                        byte[] buffer = new byte[64 << 10];
                        long remaining = source.decodedSize();
                        while (remaining > 0) {
                            monitor.checkCancelled();
                            int count = lz4.read(buffer, 0, (int) Math.min(buffer.length, remaining));
                            if (count < 0) throw new IOException("Truncated LZ4 output");
                            output.write(buffer, 0, count);
                            remaining -= count;
                        }
                        monitor.checkCancelled();
                        if (lz4.read() != -1 || lz4.getBytesRead() != source.storedSize()) {
                            throw new IOException("LZ4 output or input size mismatch");
                        }
                    }
                }, monitor);
        }
        catch (IOException e) {
            throw new IOException("LZ4 decode failed: " + e.getMessage(), e);
        }
        if (decoded.length() != source.decodedSize()) {
            decoded.close();
            throw new IOException("Cached LZ4 size mismatch");
        }
        return decoded;
    }

    @Override
    public ByteProvider getByteProvider(GFile file, TaskMonitor monitor)
            throws IOException, CancelledException {
        monitor.checkCancelled();
        if (isClosed()) throw new IOException("Engine filesystem is closed");
        Entry entry = index.getMetadata(file);
        if (entry == null) return null;
        if (entry.bytes() != null) return new ByteArrayProvider(entry.bytes(), file.getFSRL());
        var module = entry.module();
        var source = module.source();
        if ("none".equals(source.compression())) {
            return new ByteProviderWrapper(provider, source.offset(), module.elf().size(),
                file.getFSRL());
        }
        // A cached provider must outlive this method; a plain ByteProviderWrapper does not
        // close its delegate, so return a wrapper that owns this one decoded cache handle.
        ByteProvider decoded = decode(source, monitor);
        return new ByteProviderWrapper(decoded, 0, module.elf().size(), file.getFSRL()) {
            @Override
            public void close() throws IOException {
                decoded.close();
            }
        };
    }

    @Override
    public FileAttributes getFileAttributes(GFile file, TaskMonitor monitor) {
        Entry entry = index.getMetadata(file);
        var attributes = new FileAttributes();
        if (entry != null && entry.module() != null) {
            var module = entry.module();
            attributes.add("Architecture", module.elf().architecture());
            attributes.add("SHA-256", module.sha256());
            attributes.add("Kernel sections", String.join("\n", module.elf().kernels()));
        }
        return attributes;
    }

    @Override public String getName() { return fsrl.getContainer().getName(); }
    @Override public FSRLRoot getFSRL() { return fsrl; }
    @Override public FileSystemRefManager getRefManager() { return refs; }
    @Override public boolean isClosed() { return provider == null; }
    @Override public int getFileCount() { return index.getFileCount(); }
    @Override public GFile lookup(String path) { return index.lookup(path); }
    @Override public GFile lookup(String path, Comparator<String> comparator) {
        return index.lookup(null, path, comparator);
    }
    @Override public List<GFile> getListing(GFile directory) { return index.getListing(directory); }
    @Override public void close() throws IOException {
        refs.onClose();
        index.clear();
        if (provider != null) {
            try { provider.close(); }
            finally { provider = null; }
        }
    }
}

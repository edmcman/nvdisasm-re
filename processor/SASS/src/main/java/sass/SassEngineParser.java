package sass;

import java.io.IOException;
import java.io.StringReader;
import java.nio.ByteBuffer;
import java.nio.charset.CodingErrorAction;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.util.ArrayList;
import java.util.HexFormat;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import com.google.gson.GsonBuilder;
import com.google.gson.JsonObject;
import com.google.gson.JsonParser;
import com.google.gson.stream.JsonReader;
import com.google.gson.stream.JsonToken;

import ghidra.app.util.bin.BinaryReader;
import ghidra.app.util.bin.ByteProvider;
import ghidra.app.util.bin.ByteProviderWrapper;
import ghidra.util.exception.CancelledException;
import ghidra.util.task.TaskMonitor;

/** Bounded recovery of CUDA objects; no interpretation of TensorRT graphs or weights. */
public final class SassEngineParser {
    public static final long OBJECT_LIMIT = 128L << 20;
    public static final long TOTAL_LIMIT = 512L << 20;
    private static final long METADATA_LIMIT = 1L << 20;
    private static final int FATBIN_MAGIC = 0xba55ed50;
    private static final int ENGINE_MAGIC = 0x74727466; // "ftrt", TensorRT plan header
    private static final int CHUNK = 64 << 10;

    public record Header(long engineOffset, byte[] metadata) {}
    public record Elf(long size, String architecture, List<String> kernels) {}
    public record Source(long offset, long storedSize, long decodedSize, String compression) {}
    public record Occurrence(Long fatbinOffset, Long recordOffset, long offset,
            long storedSize, long decodedSize, long cubinSize, String compression,
            String architecture, List<String> kernels, String sha256, String path) {}
    public record Diagnostic(long offset, String candidate, String message) {}
    public record Module(String path, String sha256, Elf elf, Source source) {}
    public record Result(Header header, List<Module> modules, List<Occurrence> occurrences,
            List<Diagnostic> diagnostics, long decodedBytes) {
        public byte[] manifest() {
            var data = new LinkedHashMap<String, Object>();
            data.put("schema", 1);
            data.put("engineOffset", header.engineOffset());
            data.put("objectLimit", OBJECT_LIMIT);
            data.put("totalLimit", TOTAL_LIMIT);
            data.put("decodedBytes", decodedBytes);
            data.put("modules", modules);
            data.put("occurrences", occurrences);
            data.put("diagnostics", diagnostics);
            return (new GsonBuilder().setPrettyPrinting().serializeNulls().create().toJson(data) +
                "\n").getBytes(StandardCharsets.UTF_8);
        }
    }

    /** Returns a caller-owned decoded provider, usually backed by Ghidra's derived-file cache. */
    @FunctionalInterface
    public interface Decoder {
        ByteProvider decode(Source source, TaskMonitor monitor)
            throws IOException, CancelledException;
    }

    private final ByteProvider provider;
    private final Decoder decoder;
    private final TaskMonitor monitor;
    private final Map<String, Module> modules = new LinkedHashMap<>();
    private final List<Occurrence> occurrences = new ArrayList<>();
    private final List<Diagnostic> diagnostics = new ArrayList<>();
    private long decodedBytes;

    private SassEngineParser(ByteProvider provider, Decoder decoder, TaskMonitor monitor) {
        this.provider = provider;
        this.decoder = decoder;
        this.monitor = monitor;
    }

    /** A JSON prefix alone is insufficient: the following bytes must have an engine header. */
    public static Header header(ByteProvider provider, TaskMonitor monitor)
            throws IOException, CancelledException {
        monitor.checkCancelled();
        if (engineHeader(provider, 0)) return new Header(0, null);
        if (provider.length() < 4) return null;
        long size = new BinaryReader(provider, true).readUnsignedInt(0);
        if (size == 0 || size > METADATA_LIMIT || !within(4, size + 24, provider.length())) {
            return null;
        }
        if (!engineHeader(provider, 4 + size)) return null;
        byte[] bytes = provider.readBytes(4, size);
        try {
            String text = StandardCharsets.UTF_8.newDecoder()
                .onMalformedInput(CodingErrorAction.REPORT)
                .onUnmappableCharacter(CodingErrorAction.REPORT)
                .decode(ByteBuffer.wrap(bytes)).toString();
            // JsonParser otherwise accepts comments, single quotes and trailing material.
            // Use the streaming validator first; it also bounds nesting before building a tree.
            JsonReader reader = new JsonReader(new StringReader(text));
            reader.setLenient(false);
            int depth = 0;
            do {
                switch (reader.peek()) {
                    case BEGIN_OBJECT -> { reader.beginObject(); depth++; }
                    case BEGIN_ARRAY -> { reader.beginArray(); depth++; }
                    case END_OBJECT -> { reader.endObject(); depth--; }
                    case END_ARRAY -> { reader.endArray(); depth--; }
                    case NAME -> reader.nextName();
                    case STRING, NUMBER -> reader.nextString();
                    case BOOLEAN -> reader.nextBoolean();
                    case NULL -> reader.nextNull();
                    default -> { return null; }
                }
                if (depth > 64) return null;
            } while (depth > 0);
            if (reader.peek() != JsonToken.END_DOCUMENT) return null;
            var json = JsonParser.parseString(text);
            if (!json.isJsonObject()) return null;
            JsonObject object = json.getAsJsonObject();
            if (!object.has("description") || !object.get("description").isJsonPrimitive() ||
                !object.getAsJsonPrimitive("description").isString() ||
                !object.has("author") || !object.get("author").isJsonPrimitive() ||
                !object.getAsJsonPrimitive("author").isString() ||
                !"Ultralytics".equals(object.get("author").getAsString())) return null;
            monitor.checkCancelled();
            return new Header(4 + size, bytes);
        }
        catch (IOException | IllegalArgumentException |
                com.google.gson.JsonParseException e) {
            return null;
        }
    }

    private static boolean engineHeader(ByteProvider provider, long offset) throws IOException {
        if (!within(offset, 24, provider.length())) return false;
        BinaryReader reader = new BinaryReader(provider, true);
        // The serialization version is private and varies; only reserved fields and the
        // known outer header are checked. A truncated plan remains browsable with diagnostics.
        return reader.readInt(offset) == ENGINE_MAGIC &&
            reader.readInt(offset + 4) == 0 && reader.readLong(offset + 8) > 0 &&
            reader.readLong(offset + 16) >= 24;
    }

    public static Result scan(ByteProvider provider, Decoder decoder, TaskMonitor monitor)
            throws IOException, CancelledException {
        Header header = header(provider, monitor);
        if (header == null) throw new IOException("Not a TensorRT engine or Ultralytics engine");
        return new SassEngineParser(provider, decoder, monitor).scan(header);
    }

    private Result scan(Header header) throws IOException, CancelledException {
        long length = provider.length();
        long declared = new BinaryReader(provider, true).readLong(header.engineOffset() + 16);
        long available = length - header.engineOffset();
        if (declared != available) {
            reject(header.engineOffset(), "engine", "Declared engine size " + declared +
                " differs from available bytes " + available);
        }
        long end = header.engineOffset() + Math.min(declared, available);
        monitor.initialize(end);
        monitor.setMessage("Recovering CUDA modules from engine");
        long cursor = header.engineOffset() + 24;
        while (cursor <= end - 4) {
            monitor.checkCancelled();
            byte[] bytes = provider.readBytes(cursor, Math.min(CHUNK, end - cursor));
            int i = 0;
            for (; i <= bytes.length - 4; i++) {
                if ((i & 4095) == 0) monitor.checkCancelled();
                int magic = (bytes[i] & 255) | ((bytes[i + 1] & 255) << 8) |
                    ((bytes[i + 2] & 255) << 16) | ((bytes[i + 3] & 255) << 24);
                long offset = cursor + i;
                if (magic == FATBIN_MAGIC) {
                    cursor = fatbin(offset, end);
                    break;
                }
                if (magic == 0x464c457f) {
                    try (var view = new ByteProviderWrapper(provider, offset, end - offset)) {
                        Elf elf = elf(view, monitor);
                        reserve(elf.size());
                        accept(view, elf, new Source(offset, elf.size(), elf.size(), "none"),
                            null, null);
                        cursor = offset + elf.size();
                    }
                    catch (IOException e) {
                        reject(offset, "ELF", e.getMessage());
                        cursor = offset + 4;
                    }
                    break;
                }
            }
            if (i > bytes.length - 4) cursor += bytes.length - 3;
            monitor.setProgress(cursor);
        }
        return new Result(header, List.copyOf(modules.values()), List.copyOf(occurrences),
            List.copyOf(diagnostics), decodedBytes);
    }

    /** Once a fatbin envelope is recognized, do not carve ELF signatures from its payload. */
    private long fatbin(long offset, long limit) throws IOException, CancelledException {
        BinaryReader reader = new BinaryReader(provider, true);
        if (!within(offset, 16, limit)) {
            reject(offset, "fatbin", "Truncated fatbin header");
            return limit;
        }
        int version = reader.readUnsignedShort(offset + 4);
        int headerSize = reader.readUnsignedShort(offset + 6);
        long payloadSize = reader.readLong(offset + 8);
        if (headerSize < 16 || !within(offset, headerSize, limit) || payloadSize < 0 ||
                !within(offset + headerSize, payloadSize, limit)) {
            reject(offset, "fatbin", "Invalid or truncated fatbin size");
            return limit;
        }
        long end = offset + headerSize + payloadSize;
        if (version != 1) {
            reject(offset, "fatbin", "Unsupported fatbin version " + version);
            return end;
        }
        long record = offset + headerSize;
        while (record < end) {
            monitor.checkCancelled();
            if (!within(record, 16, end)) {
                reject(record, "fatbin record", "Truncated record header");
                break;
            }
            int kind = reader.readUnsignedShort(record);
            int recordVersion = reader.readUnsignedShort(record + 2);
            long recordHeader = reader.readUnsignedInt(record + 4);
            long stored = reader.readLong(record + 8);
            if (recordHeader < 16 || !within(record, recordHeader, end) || stored < 0 ||
                    !within(record + recordHeader, stored, end)) {
                reject(record, "fatbin record", "Invalid or truncated record size");
                break;
            }
            long payload = record + recordHeader;
            long next = payload + stored;
            try {
                if (kind != 2 || recordVersion != 0x101 || recordHeader < 64) {
                    throw new IOException("Unsupported record kind/version/header: " +
                        kind + "/" + recordVersion + "/" + recordHeader);
                }
                long flags = reader.readLong(record + 40);
                // Known CUDA flags: 64-bit, debug, producer/OS flags, LZ4 compression.
                if ((flags & ~0x201fL) != 0) {
                    throw new IOException("Unsupported fatbin flags 0x" +
                        Long.toHexString(flags));
                }
                boolean compressed = (flags & 0x2000) != 0;
                long encoded = compressed ? reader.readLong(record + 16) : stored;
                long decoded = compressed ? reader.readLong(record + 56) : stored;
                if (encoded <= 0 || encoded > stored || decoded <= 0 ||
                        (compressed && stored - encoded > 7)) {
                    throw new IOException("Invalid compressed or decoded size");
                }
                reserve(decoded);
                Source source = new Source(payload, encoded, decoded, compressed ? "lz4" : "none");
                try (ByteProvider view = compressed ? decoder.decode(source, monitor) :
                        new ByteProviderWrapper(provider, payload, decoded)) {
                    if (view.length() != decoded) throw new IOException("Decoded size mismatch");
                    Elf elf = elf(view, monitor);
                    long arch = reader.readUnsignedInt(record + 28);
                    if (!elf.architecture().equals("SM" + arch)) {
                        throw new IOException("Fatbin and ELF architectures disagree");
                    }
                    accept(view, elf, source, offset, record);
                }
            }
            catch (IOException e) {
                reject(record, "fatbin record", e.getMessage());
            }
            record = next;
        }
        return end;
    }

    private void reserve(long size) throws IOException {
        if (size <= 0 || size > OBJECT_LIMIT) throw new IOException("128 MiB object limit exceeded");
        if (size > TOTAL_LIMIT - decodedBytes) throw new IOException("512 MiB total limit exceeded");
        // Count every decode attempt, including rejected objects and duplicates.
        decodedBytes += size;
    }

    private void accept(ByteProvider view, Elf elf, Source source, Long fatbin, Long record)
            throws IOException, CancelledException {
        MessageDigest digest;
        try {
            digest = MessageDigest.getInstance("SHA-256");
        }
        catch (NoSuchAlgorithmException e) {
            throw new AssertionError(e);
        }
        for (long offset = 0; offset < elf.size(); offset += CHUNK) {
            monitor.checkCancelled();
            digest.update(view.readBytes(offset, Math.min(CHUNK, elf.size() - offset)));
        }
        String sha = HexFormat.of().formatHex(digest.digest());
        String path = "cubins/" + elf.architecture() + "/module_" + sha + ".cubin";
        modules.putIfAbsent(sha, new Module(path, sha, elf, source));
        occurrences.add(new Occurrence(fatbin, record, source.offset(), source.storedSize(),
            source.decodedSize(), elf.size(), source.compression(), elf.architecture(),
            elf.kernels(), sha, path));
    }

    private void reject(long offset, String candidate, String message) throws IOException {
        if (diagnostics.size() >= 10000) throw new IOException("Too many rejected candidates");
        diagnostics.add(new Diagnostic(offset, candidate, message));
    }

    public static boolean within(long offset, long size, long limit) {
        return offset >= 0 && size >= 0 && offset <= limit && size <= limit - offset;
    }

    private static void range(long offset, long size, long limit) throws IOException {
        if (!within(offset, size, limit)) throw new IOException("ELF range outside object");
    }

    private record Section(long name, long type, long flags, long offset, long size,
            long link, long info, long alignment, long entrySize) {}

    /** Validate the complete file layout, rather than accepting an embedded ELF magic string. */
    public static Elf elf(ByteProvider provider, TaskMonitor monitor)
            throws IOException, CancelledException {
        monitor.checkCancelled();
        long limit = Math.min(provider.length(), OBJECT_LIMIT);
        range(0, 52, limit);
        BinaryReader reader = new BinaryReader(provider, true);
        int bits = reader.readUnsignedByte(4);
        if (reader.readInt(0) != 0x464c457f || (bits != 1 && bits != 2) ||
                reader.readUnsignedByte(5) != 1 || reader.readUnsignedByte(6) != 1 ||
                reader.readUnsignedShort(18) != 190 ||
                (reader.readUnsignedShort(16) != 1 && reader.readUnsignedShort(16) != 2)) {
            throw new IOException("Not a little-endian CUDA ELF");
        }
        boolean wide = bits == 2;
        int header = wide ? 64 : 52;
        range(0, header, limit);
        long phoff = wide ? reader.readLong(32) : reader.readUnsignedInt(28);
        long shoff = wide ? reader.readLong(40) : reader.readUnsignedInt(32);
        long flags = reader.readUnsignedInt(wide ? 48 : 36);
        // CUDA ELF ABI v7 stores the target in bits 0-7; v8 moved it to bits 8-15.
        long sm = (flags >> (reader.readUnsignedByte(8) >= 8 ? 8 : 0)) & 255;
        int ehsize = reader.readUnsignedShort(wide ? 52 : 40);
        int phsize = reader.readUnsignedShort(wide ? 54 : 42);
        int phnum = reader.readUnsignedShort(wide ? 56 : 44);
        int shsize = reader.readUnsignedShort(wide ? 58 : 46);
        int shnum = reader.readUnsignedShort(wide ? 60 : 48);
        int namesIndex = reader.readUnsignedShort(wide ? 62 : 50);
        if (ehsize != header || shnum == 0 || shsize != (wide ? 64 : 40) ||
                namesIndex == 0 || namesIndex >= shnum || phnum == 0xffff ||
                (phnum > 0 && phsize != (wide ? 56 : 32)) || sm == 0) {
            throw new IOException("Invalid or unsupported ELF table header");
        }
        range(shoff, (long) shsize * shnum, limit);
        range(phoff, (long) phsize * phnum, limit);
        if (shoff < header || (phnum > 0 && phoff < header)) {
            throw new IOException("ELF tables overlap header");
        }
        long extent = Math.max(header, Math.max(shoff + (long) shsize * shnum,
            phoff + (long) phsize * phnum));
        var sections = new ArrayList<Section>();
        for (int i = 0; i < shnum; i++) {
            monitor.checkCancelled();
            long at = shoff + (long) shsize * i;
            Section section = new Section(reader.readUnsignedInt(at),
                reader.readUnsignedInt(at + 4),
                wide ? reader.readLong(at + 8) : reader.readUnsignedInt(at + 8),
                wide ? reader.readLong(at + 24) : reader.readUnsignedInt(at + 16),
                wide ? reader.readLong(at + 32) : reader.readUnsignedInt(at + 20),
                reader.readUnsignedInt(at + (wide ? 40 : 24)),
                reader.readUnsignedInt(at + (wide ? 44 : 28)),
                wide ? reader.readLong(at + 48) : reader.readUnsignedInt(at + 32),
                wide ? reader.readLong(at + 56) : reader.readUnsignedInt(at + 36));
            if (section.size() < 0 || section.offset() < 0 || section.entrySize() < 0 ||
                    section.alignment() < 0 ||
                    (section.alignment() & (section.alignment() - 1)) != 0 ||
                    section.link() >= shnum) throw new IOException("Invalid ELF section");
            if (i == 0 && section.type() != 0) throw new IOException("Missing ELF null section");
            if (section.type() != 8 && section.type() != 0) {
                range(section.offset(), section.size(), limit);
                extent = Math.max(extent, section.offset() + section.size());
            }
            if (section.entrySize() > 0 && section.size() % section.entrySize() != 0) {
                throw new IOException("Invalid ELF section entry size");
            }
            sections.add(section);
        }
        for (int i = 0; i < phnum; i++) {
            monitor.checkCancelled();
            long at = phoff + (long) phsize * i;
            long offset = wide ? reader.readLong(at + 8) : reader.readUnsignedInt(at + 4);
            long size = wide ? reader.readLong(at + 32) : reader.readUnsignedInt(at + 16);
            long memory = wide ? reader.readLong(at + 40) : reader.readUnsignedInt(at + 20);
            range(offset, size, limit);
            if (memory < size) throw new IOException("Invalid ELF segment memory size");
            extent = Math.max(extent, offset + size);
        }
        Section names = sections.get(namesIndex);
        if (names.type() != 3 || names.size() == 0) throw new IOException("Invalid ELF name table");
        byte[] strings = provider.readBytes(names.offset(), names.size());
        var kernels = new ArrayList<String>();
        for (Section section : sections) {
            monitor.checkCancelled();
            if (section.name() >= strings.length) throw new IOException("Invalid ELF name offset");
            int start = (int) section.name(), end = start;
            while (end < strings.length && strings[end] != 0) {
                if ((end & 4095) == 0) monitor.checkCancelled();
                end++;
            }
            if (end == strings.length) throw new IOException("Unterminated ELF section name");
            String name = new String(strings, start, end - start, StandardCharsets.UTF_8);
            if (name.startsWith(".text.") && section.type() == 1 &&
                    (section.flags() & 4) != 0) kernels.add(name.substring(6));
            if (section.type() == 2 || section.type() == 11) {
                if (section.entrySize() != (wide ? 24 : 16) ||
                        sections.get((int) section.link()).type() != 3 ||
                        section.info() > section.size() / section.entrySize()) {
                    throw new IOException("Invalid ELF symbol table");
                }
            }
            if ((section.type() == 4 || section.type() == 9) &&
                    (section.info() >= shnum ||
                     (sections.get((int) section.link()).type() != 2 &&
                      sections.get((int) section.link()).type() != 11) ||
                     section.entrySize() != (wide ? (section.type() == 4 ? 24 : 16) :
                         (section.type() == 4 ? 12 : 8)))) {
                throw new IOException("Invalid ELF relocation table");
            }
        }
        return new Elf(extent, "SM" + sm, List.copyOf(kernels));
    }
}

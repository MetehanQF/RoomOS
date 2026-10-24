package com.metehantech.roomos.auth

import java.io.File
import java.nio.file.Files
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test

class FileSessionStoreTest {
    private lateinit var directory: File
    private lateinit var cipher: FakeCipher

    @Before
    fun setup() {
        directory = Files.createTempDirectory("roomos-session-test").toFile()
        cipher = FakeCipher()
    }

    @After
    fun cleanup() {
        directory.deleteRecursively()
    }

    @Test
    fun `session survives store recreation without plaintext at rest`() {
        val token = "native-secret-token-1234567890"
        FileSessionStore(directory, cipher).save(token)
        val bytes = File(directory, "native-session.bin").readBytes()
        assertFalse(bytes.decodeToString().contains(token))
        assertEquals(token, FileSessionStore(directory, cipher).load())
    }

    @Test
    fun `corrupt ciphertext fails closed and clears key`() {
        File(directory, "native-session.bin").writeBytes(byteArrayOf(1, 2, 3))
        assertNull(FileSessionStore(directory, cipher).load())
        assertTrue(cipher.cleared)
        assertFalse(File(directory, "native-session.bin").exists())
    }

    private class FakeCipher : CipherBox {
        var cleared = false
        override fun encrypt(plaintext: ByteArray) = byteArrayOf(7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18) to plaintext.map { (it.toInt() xor 0x5A).toByte() }.toByteArray()
        override fun decrypt(iv: ByteArray, ciphertext: ByteArray) = ciphertext.map { (it.toInt() xor 0x5A).toByte() }.toByteArray()
        override fun clear() { cleared = true }
    }
}

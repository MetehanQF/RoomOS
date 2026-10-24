package com.metehantech.roomos.auth

import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import java.io.DataInputStream
import java.io.DataOutputStream
import java.io.File
import java.security.KeyStore
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec

interface SessionStore {
    fun load(): String?
    fun save(token: String)
    fun clear()
}

interface CipherBox {
    fun encrypt(plaintext: ByteArray): Pair<ByteArray, ByteArray>
    fun decrypt(iv: ByteArray, ciphertext: ByteArray): ByteArray
    fun clear()
}

class FileSessionStore(private val directory: File, private val cipherBox: CipherBox) : SessionStore {
    private val file = File(directory, "native-session.bin")

    override fun load(): String? {
        if (!file.isFile) return null
        return try {
            DataInputStream(file.inputStream().buffered()).use { input ->
                val ivLength = input.readInt()
                require(ivLength in 12..32)
                val iv = ByteArray(ivLength).also(input::readFully)
                val encryptedLength = input.readInt()
                require(encryptedLength in 16..4096)
                val ciphertext = ByteArray(encryptedLength).also(input::readFully)
                cipherBox.decrypt(iv, ciphertext).decodeToString()
            }
        } catch (_: Exception) {
            clear()
            null
        }
    }

    override fun save(token: String) {
        require(token.length in 16..512)
        directory.mkdirs()
        val (iv, ciphertext) = cipherBox.encrypt(token.encodeToByteArray())
        val temporary = File(directory, ".native-session.tmp")
        DataOutputStream(temporary.outputStream().buffered()).use { output ->
            output.writeInt(iv.size)
            output.write(iv)
            output.writeInt(ciphertext.size)
            output.write(ciphertext)
            output.flush()
        }
        if (!temporary.renameTo(file)) {
            temporary.delete()
            error("Unable to persist native session")
        }
    }

    override fun clear() {
        file.delete()
        cipherBox.clear()
    }
}

class AndroidKeystoreCipher : CipherBox {
    private val alias = "roomos_native_session_v1"

    private fun key(): SecretKey {
        val store = KeyStore.getInstance("AndroidKeyStore").apply { load(null) }
        (store.getKey(alias, null) as? SecretKey)?.let { return it }
        return KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, "AndroidKeyStore").run {
            init(
                KeyGenParameterSpec.Builder(
                    alias,
                    KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT,
                )
                    .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                    .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                    .setRandomizedEncryptionRequired(true)
                    .build(),
            )
            generateKey()
        }
    }

    override fun encrypt(plaintext: ByteArray): Pair<ByteArray, ByteArray> {
        val cipher = Cipher.getInstance("AES/GCM/NoPadding")
        cipher.init(Cipher.ENCRYPT_MODE, key())
        return cipher.iv to cipher.doFinal(plaintext)
    }

    override fun decrypt(iv: ByteArray, ciphertext: ByteArray): ByteArray {
        val cipher = Cipher.getInstance("AES/GCM/NoPadding")
        cipher.init(Cipher.DECRYPT_MODE, key(), GCMParameterSpec(128, iv))
        return cipher.doFinal(ciphertext)
    }

    override fun clear() {
        val store = KeyStore.getInstance("AndroidKeyStore").apply { load(null) }
        if (store.containsAlias(alias)) store.deleteEntry(alias)
    }
}

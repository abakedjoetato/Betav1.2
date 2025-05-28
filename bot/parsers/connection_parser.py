"""
Emerald's Killfeed - Player Connection Lifecycle Parser
Complete rebuild implementing 4-event lifecycle tracking with live counts
"""

import asyncio
import logging
import re
from datetime import datetime, timezone
from typing import Dict, List, Optional, Any, Set
import discord
from bot.utils.embed_factory import EmbedFactory

logger = logging.getLogger(__name__)

class ConnectionLifecycleParser:
    """
    PLAYER CONNECTION + COUNT SYSTEM - Complete Rebuild
    Tracks 4 core lifecycle events: jq, j2, d1, d2
    Maintains live counts: QC = jq - j2 - d2, PC = j2 - d1
    """

    def __init__(self, bot):
        self.bot = bot
        
        # Live count tracking per server
        self.server_counts: Dict[str, Dict[str, Any]] = {}
        
        # Player state tracking per server  
        self.player_states: Dict[str, Dict[str, Set[str]]] = {}
        
        # Compile robust regex patterns for the 4 lifecycle events
        self.lifecycle_patterns = {
            # 1. Queue Join (jq) - Player enters queue
            'queue_join': re.compile(
                r'LogNet: Join request: /Game/Maps/world_0/World_0\?.*\?Name=([^&\s]+).*(?:platformid=PS5:(\w+)|eosid=\|(\w+))', 
                re.IGNORECASE
            ),
            
            # 2. Player Joined (j2) - Player successfully registered
            'player_joined': re.compile(
                r'LogOnline: Warning: Player \|(\w+) successfully registered!', 
                re.IGNORECASE
            ),
            
            # 3. Disconnect Post-Join (d1) - Standard disconnect after joining
            'disconnect_post_join': re.compile(
                r'UChannel::Close: Sending CloseBunch.*UniqueId: EOS:\|(\w+)', 
                re.IGNORECASE
            ),
            
            # 4. Disconnect Pre-Join (d2) - Disconnect from queue before joining
            'disconnect_pre_join': re.compile(
                r'UChannel::Close: Sending CloseBunch.*UniqueId: (?:PS5|EOS):\|?(\w+)', 
                re.IGNORECASE
            )
        }

    def initialize_server_tracking(self, server_key: str):
        """Initialize tracking structures for a server"""
        if server_key not in self.server_counts:
            self.server_counts[server_key] = {
                'queue_count': 0,  # QC = jq - j2 - d2
                'player_count': 0,  # PC = j2 - d1
            }
            
        if server_key not in self.player_states:
            self.player_states[server_key] = {
                'players_queued': set(),      # Players in queue (jq)
                'players_joined': set(),      # Players who joined (j2)
                'players_disconnected_pre': set(),   # Players who disconnected from queue (d2)
                'players_disconnected_post': set()   # Players who disconnected after joining (d1)
            }

    async def parse_lifecycle_event(self, line: str, server_key: str, guild_id: int) -> Optional[Dict[str, Any]]:
        """Parse a single log line for lifecycle events"""
        self.initialize_server_tracking(server_key)
        
        # Extract player ID from the line for tracking
        player_id = None
        event_type = None
        player_name = None
        
        # Check for Queue Join (jq)
        if match := self.lifecycle_patterns['queue_join'].search(line):
            player_name = match.group(1)
            player_id = match.group(2) or match.group(3)  # PS5 or EOS ID
            event_type = 'jq'
            
            if player_id:
                self.player_states[server_key]['players_queued'].add(player_id)
                await self._update_live_counts(server_key)
                logger.info(f"🟡 Queue Join: {player_name} ({player_id}) joined queue")
        
        # Check for Player Joined (j2)
        elif match := self.lifecycle_patterns['player_joined'].search(line):
            player_id = match.group(1)
            event_type = 'j2'
            
            self.player_states[server_key]['players_joined'].add(player_id)
            await self._update_live_counts(server_key)
            logger.info(f"🟢 Player Joined: {player_id} successfully registered")
            
            # Create join embed
            return await self._create_join_embed(player_id, player_name)
        
        # Check for Disconnect (d1 or d2)
        elif match := self.lifecycle_patterns['disconnect_post_join'].search(line):
            player_id = match.group(1)
            
            # Determine if this is d1 (post-join) or d2 (pre-join)
            if player_id in self.player_states[server_key]['players_joined']:
                event_type = 'd1'
                self.player_states[server_key]['players_disconnected_post'].add(player_id)
                logger.info(f"🔴 Post-Join Disconnect: {player_id} left after joining")
                
                # Create leave embed
                return await self._create_leave_embed(player_id)
                
            elif player_id in self.player_states[server_key]['players_queued']:
                event_type = 'd2'
                self.player_states[server_key]['players_disconnected_pre'].add(player_id)
                logger.info(f"🟠 Pre-Join Disconnect: {player_id} left queue before joining")
            
            await self._update_live_counts(server_key)
        
        # Check for Pre-Join Disconnect (d2 alternative pattern)
        elif match := self.lifecycle_patterns['disconnect_pre_join'].search(line):
            player_id = match.group(1)
            
            # Only count as d2 if player was in queue but never joined
            if (player_id in self.player_states[server_key]['players_queued'] and 
                player_id not in self.player_states[server_key]['players_joined']):
                event_type = 'd2'
                self.player_states[server_key]['players_disconnected_pre'].add(player_id)
                await self._update_live_counts(server_key)
                logger.info(f"🟠 Pre-Join Disconnect: {player_id} left queue before joining")
        
        return None

    async def _update_live_counts(self, server_key: str):
        """Update live player and queue counts using the formulas"""
        states = self.player_states[server_key]
        counts = self.server_counts[server_key]
        
        # QC (Queue Count) = jq - j2 - d2
        jq = len(states['players_queued'])
        j2 = len(states['players_joined']) 
        d2 = len(states['players_disconnected_pre'])
        queue_count = max(0, jq - j2 - d2)
        
        # PC (Player Count) = j2 - d1
        d1 = len(states['players_disconnected_post'])
        player_count = max(0, j2 - d1)
        
        counts['queue_count'] = queue_count
        counts['player_count'] = player_count
        
        logger.info(f"📊 Live Counts - Players: {player_count}, Queue: {queue_count}")
        
        # Update voice channels with new counts
        await self._update_voice_channels(server_key, player_count, queue_count)

    async def _update_voice_channels(self, server_key: str, player_count: int, queue_count: int):
        """Update voice channel names with current player and queue counts"""
        try:
            # Extract guild_id from server_key (format: guild_id_server_id)
            guild_id = int(server_key.split('_')[0])
            guild = self.bot.get_guild(guild_id)
            
            if not guild:
                return
                
            # Find voice channels to update
            for channel in guild.voice_channels:
                if 'players:' in channel.name.lower() or 'queue:' in channel.name.lower():
                    new_name = f"Players: {player_count} / Queue: {queue_count}"
                    if channel.name != new_name:
                        await channel.edit(name=new_name)
                        logger.info(f"🔊 Updated voice channel: {new_name}")
                        
        except Exception as e:
            logger.error(f"Failed to update voice channels: {e}")

    async def _create_join_embed(self, player_id: str, player_name: Optional[str] = None) -> Dict[str, Any]:
        """Create themed embed for player join event"""
        embed_data = {
            'event_type': 'player_join',
            'player_name': player_name or player_id,
            'player_id': player_id,
            'timestamp': datetime.now(timezone.utc),
            'messages': [
                f"🎮 {player_name or player_id} joined the server!",
                f"🌟 Welcome {player_name or player_id} to the battlefield!",
                f"⚔️ {player_name or player_id} has entered the game!",
                f"🎯 {player_name or player_id} is ready for action!"
            ]
        }
        
        embed, file_attachment = await EmbedFactory.build('player_join', embed_data)
        return {'embed': embed, 'file': file_attachment}

    async def _create_leave_embed(self, player_id: str, player_name: Optional[str] = None) -> Dict[str, Any]:
        """Create themed embed for player leave event"""
        embed_data = {
            'event_type': 'player_leave', 
            'player_name': player_name or player_id,
            'player_id': player_id,
            'timestamp': datetime.now(timezone.utc),
            'messages': [
                f"👋 {player_name or player_id} left the server",
                f"🚪 {player_name or player_id} disconnected from the battlefield",
                f"⏰ {player_name or player_id} has ended their session",
                f"🔚 {player_name or player_id} signed off"
            ]
        }
        
        embed, file_attachment = await EmbedFactory.build('player_leave', embed_data)
        return {'embed': embed, 'file': file_attachment}

    def get_live_counts(self, server_key: str) -> Dict[str, int]:
        """Get current live player and queue counts for a server"""
        self.initialize_server_tracking(server_key)
        return self.server_counts[server_key].copy()

    def reset_server_counts(self, server_key: str):
        """Reset all counts for a server (useful for log rotation)"""
        if server_key in self.server_counts:
            del self.server_counts[server_key]
        if server_key in self.player_states:
            del self.player_states[server_key]
        logger.info(f"🔄 Reset player counts for server {server_key}")
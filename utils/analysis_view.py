"""
Interactive paginated view for user analysis results.
Provides navigation buttons and beautiful formatting.
"""

import discord
from typing import List, Dict, Any
import io


class AnalysisView(discord.ui.View):
    """Interactive paginated view for displaying comprehensive user analysis."""
    
    def __init__(self, pages: List[discord.Embed], full_report: str, timeout: float = 900):
        """
        Initialize the analysis view.
        
        Args:
            pages: List of embed pages to display
            full_report: Full text report for download
            timeout: View timeout in seconds (default 15 minutes)
        """
        super().__init__(timeout=timeout)
        self.pages = pages
        self.full_report = full_report
        self.current_page = 0
        self.message: discord.Message = None
        
        # Update button states
        self._update_buttons()
    
    def _update_buttons(self):
        """Update button states based on current page."""
        # Disable previous button on first page
        self.previous_button.disabled = (self.current_page == 0)
        
        # Disable next button on last page
        self.next_button.disabled = (self.current_page == len(self.pages) - 1)
        
        # Update page indicator
        self.page_indicator.label = f"Page {self.current_page + 1}/{len(self.pages)}"
    
    @discord.ui.button(label="◀️ Previous", style=discord.ButtonStyle.primary, custom_id="previous")
    async def previous_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Go to previous page."""
        if self.current_page > 0:
            self.current_page -= 1
            self._update_buttons()
            await interaction.response.edit_message(embed=self.pages[self.current_page], view=self)
    
    @discord.ui.button(label="Page 1/1", style=discord.ButtonStyle.secondary, custom_id="page_indicator", disabled=True)
    async def page_indicator(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Page indicator (non-interactive)."""
        pass
    
    @discord.ui.button(label="Next ▶️", style=discord.ButtonStyle.primary, custom_id="next")
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Go to next page."""
        if self.current_page < len(self.pages) - 1:
            self.current_page += 1
            self._update_buttons()
            await interaction.response.edit_message(embed=self.pages[self.current_page], view=self)
    
    @discord.ui.button(label="🏠 Home", style=discord.ButtonStyle.success, custom_id="home")
    async def home_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Go to first page."""
        self.current_page = 0
        self._update_buttons()
        await interaction.response.edit_message(embed=self.pages[self.current_page], view=self)
    
    @discord.ui.button(label="📥 Download Report", style=discord.ButtonStyle.secondary, custom_id="download")
    async def download_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Download full report as text file."""
        file_content = self.full_report.encode('utf-8')
        file = discord.File(io.BytesIO(file_content), filename="analysis_report.txt")
        
        await interaction.response.send_message(
            "📄 Here's your complete analysis report!",
            file=file,
            ephemeral=True
        )
    
    async def on_timeout(self):
        """Called when the view times out."""
        # Disable all buttons
        for item in self.children:
            item.disabled = True
        
        # Update message if it exists
        if self.message:
            try:
                await self.message.edit(view=self)
            except:
                pass


def create_analysis_embeds(analysis_data: Dict[str, Any], user_name: str, message_count: int) -> List[discord.Embed]:
    """
    Create beautiful paginated embeds from analysis data.
    
    Args:
        analysis_data: Comprehensive analysis data
        user_name: Display name of analyzed user
        message_count: Number of messages analyzed
    
    Returns:
        List of formatted embed pages
    """
    pages = []
    
    # Page 1: Overview & Communication Style
    embed1 = discord.Embed(
        title=f"📊 Analysis: {user_name}",
        description=f"**Comprehensive personality and behavior analysis**\n*Based on {message_count:,} messages*",
        color=discord.Color.blue()
    )
    
    if "overview" in analysis_data:
        embed1.add_field(
            name="📝 Overview",
            value=analysis_data["overview"][:1024],
            inline=False
        )
    
    if "communication_style" in analysis_data:
        embed1.add_field(
            name="💬 Communication Style",
            value=analysis_data["communication_style"][:1024],
            inline=False
        )
    
    embed1.set_footer(text="Page 1 • Use buttons below to navigate")
    pages.append(embed1)
    
    # Page 2: Personality & Interests
    embed2 = discord.Embed(
        title=f"🎭 Personality Profile: {user_name}",
        color=discord.Color.green()
    )
    
    if "personality_traits" in analysis_data:
        embed2.add_field(
            name="✨ Personality Traits",
            value=analysis_data["personality_traits"][:1024],
            inline=False
        )
    
    if "interests" in analysis_data:
        embed2.add_field(
            name="🎯 Interests & Topics",
            value=analysis_data["interests"][:1024],
            inline=False
        )
    
    embed2.set_footer(text="Page 2 • Use buttons below to navigate")
    pages.append(embed2)
    
    # Page 3: Behavior & Activity
    embed3 = discord.Embed(
        title=f"📈 Behavioral Analysis: {user_name}",
        color=discord.Color.purple()
    )
    
    if "behavioral_patterns" in analysis_data:
        embed3.add_field(
            name="🔄 Behavioral Patterns",
            value=analysis_data["behavioral_patterns"][:1024],
            inline=False
        )
    
    if "activity_patterns" in analysis_data:
        embed3.add_field(
            name="⏰ Activity Patterns",
            value=analysis_data["activity_patterns"][:1024],
            inline=False
        )
    
    embed3.set_footer(text="Page 3 • Use buttons below to navigate")
    pages.append(embed3)
    
    # Page 4: Social Dynamics & Insights
    embed4 = discord.Embed(
        title=f"🤝 Social Dynamics: {user_name}",
        color=discord.Color.orange()
    )
    
    if "social_dynamics" in analysis_data:
        embed4.add_field(
            name="👥 Social Interactions",
            value=analysis_data["social_dynamics"][:1024],
            inline=False
        )
    
    if "unique_insights" in analysis_data:
        embed4.add_field(
            name="💡 Unique Insights",
            value=analysis_data["unique_insights"][:1024],
            inline=False
        )
    
    if "vocabulary" in analysis_data:
        embed4.add_field(
            name="📚 Vocabulary & Expression",
            value=analysis_data["vocabulary"][:1024],
            inline=False
        )
    
    embed4.set_footer(text="Page 4 • Download full report for complete details")
    pages.append(embed4)
    
    return pages


def format_full_report(analysis_data: Dict[str, Any], user_name: str, message_count: int) -> str:
    """
    Format complete analysis as downloadable text report.
    
    Args:
        analysis_data: Comprehensive analysis data
        user_name: Display name of analyzed user
        message_count: Number of messages analyzed
    
    Returns:
        Formatted text report
    """
    report = f"""
╔══════════════════════════════════════════════════════════════╗
║          COMPREHENSIVE USER ANALYSIS REPORT                  ║
╚══════════════════════════════════════════════════════════════╝

User: {user_name}
Messages Analyzed: {message_count:,}
Generated: {discord.utils.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}

═══════════════════════════════════════════════════════════════

📝 OVERVIEW
───────────────────────────────────────────────────────────────
{analysis_data.get('overview', 'N/A')}

💬 COMMUNICATION STYLE
───────────────────────────────────────────────────────────────
{analysis_data.get('communication_style', 'N/A')}

✨ PERSONALITY TRAITS
───────────────────────────────────────────────────────────────
{analysis_data.get('personality_traits', 'N/A')}

🎯 INTERESTS & TOPICS
───────────────────────────────────────────────────────────────
{analysis_data.get('interests', 'N/A')}

🔄 BEHAVIORAL PATTERNS
───────────────────────────────────────────────────────────────
{analysis_data.get('behavioral_patterns', 'N/A')}

⏰ ACTIVITY PATTERNS
───────────────────────────────────────────────────────────────
{analysis_data.get('activity_patterns', 'N/A')}

👥 SOCIAL DYNAMICS
───────────────────────────────────────────────────────────────
{analysis_data.get('social_dynamics', 'N/A')}

📚 VOCABULARY & EXPRESSION
───────────────────────────────────────────────────────────────
{analysis_data.get('vocabulary', 'N/A')}

💡 UNIQUE INSIGHTS
───────────────────────────────────────────────────────────────
{analysis_data.get('unique_insights', 'N/A')}

═══════════════════════════════════════════════════════════════
End of Report
═══════════════════════════════════════════════════════════════
"""
    return report

